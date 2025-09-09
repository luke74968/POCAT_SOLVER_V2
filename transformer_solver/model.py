# model.py
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Tuple
from tensordict import TensorDict

from common.pocat_defs import FEATURE_DIM
from common.utils.common import batchify
from .pocat_env import PocatEnv

# ... (RMSNorm, Normalization, EncoderLayer 등 다른 클래스는 이전과 동일) ...
class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))
    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)
    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight

class Normalization(nn.Module):
    def __init__(self, embedding_dim, norm_type='rms', **kwargs):
        super().__init__()
        self.norm_type = norm_type
        if self.norm_type == 'layer': self.norm = nn.LayerNorm(embedding_dim)
        elif self.norm_type == 'rms': self.norm = RMSNorm(embedding_dim)
        elif self.norm_type == 'instance': self.norm = nn.InstanceNorm1d(embedding_dim, affine=True, track_running_stats=False)
        else: raise NotImplementedError
    def forward(self, x):
        if self.norm_type == 'instance': return self.norm(x.transpose(1, 2)).transpose(1, 2)
        else: return self.norm(x)

class ParallelGatedMLP(nn.Module):
    def __init__(self, hidden_size: int, **kwargs):
        super().__init__()
        inner_size = int(2 * hidden_size * 4 / 3)
        multiple_of = 256
        inner_size = multiple_of * ((inner_size + multiple_of - 1) // multiple_of)
        self.l1, self.l2, self.l3 = nn.Linear(hidden_size, inner_size, bias=False), nn.Linear(hidden_size, inner_size, bias=False), nn.Linear(inner_size, hidden_size, bias=False)
        self.act = F.silu
    def forward(self, z):
        z1, z2 = self.l1(z), self.l2(z)
        return self.l3(self.act(z1) * z2)

class FeedForward(nn.Module):
    def __init__(self, embedding_dim, ff_hidden_dim, **kwargs):
        super().__init__()
        self.W1 = nn.Linear(embedding_dim, ff_hidden_dim)
        self.W2 = nn.Linear(ff_hidden_dim, embedding_dim)
    def forward(self, input1):
        return self.W2(F.relu(self.W1(input1)))

def reshape_by_heads(qkv: torch.Tensor, head_num: int) -> torch.Tensor:
    batch_s, n = qkv.size(0), qkv.size(1)
    q_reshaped = qkv.reshape(batch_s, n, head_num, -1)
    return q_reshaped.transpose(1, 2)

def multi_head_attention(q, k, v, ninf_mask=None):
    batch_s, head_num, n, key_dim = q.shape
    score = torch.matmul(q, k.transpose(2, 3))
    score_scaled = score / (key_dim ** 0.5)
    if ninf_mask is not None:
        score_scaled = score_scaled + ninf_mask[:, None, :, :].expand(batch_s, head_num, n, k.size(2))
    weights = nn.Softmax(dim=3)(score_scaled)
    out = torch.matmul(weights, v)
    out_transposed = out.transpose(1, 2)
    return out_transposed.contiguous().view(batch_s, n, head_num * key_dim)

class EncoderLayer(nn.Module):
    def __init__(self, embedding_dim, head_num, qkv_dim, ffd='siglu', **model_params):
        super().__init__()
        self.embedding_dim, self.head_num, self.qkv_dim = embedding_dim, head_num, qkv_dim
        self.Wq, self.Wk, self.Wv = nn.Linear(embedding_dim, head_num * qkv_dim, bias=False), nn.Linear(embedding_dim, head_num * qkv_dim, bias=False), nn.Linear(embedding_dim, head_num * qkv_dim, bias=False)
        self.multi_head_combine = nn.Linear(head_num * qkv_dim, embedding_dim)
        self.normalization1 = Normalization(embedding_dim, **model_params)
        if ffd == 'siglu': self.feed_forward = ParallelGatedMLP(hidden_size=embedding_dim, **model_params)
        else: self.feed_forward = FeedForward(embedding_dim=embedding_dim, **model_params)
        self.normalization2 = Normalization(embedding_dim, **model_params)
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        q, k, v = reshape_by_heads(self.Wq(x), self.head_num), reshape_by_heads(self.Wk(x), self.head_num), reshape_by_heads(self.Wv(x), self.head_num)
        mha_out = self.multi_head_combine(multi_head_attention(q, k, v))
        h = self.normalization1(x + mha_out)
        return self.normalization2(h + self.feed_forward(h))

class PocatPromptNet(nn.Module):
    def __init__(self, embedding_dim: int, prompt_feature_dim: int = 2, **kwargs):
        super().__init__()
        self.model = nn.Sequential(nn.Linear(prompt_feature_dim, embedding_dim // 2), nn.ReLU(), nn.Linear(embedding_dim // 2, embedding_dim))
    def forward(self, prompt_features: torch.Tensor) -> torch.Tensor:
        return self.model(prompt_features).unsqueeze(1)

class PocatEncoder(nn.Module):
    def __init__(self, embedding_dim: int, encoder_layer_num: int = 6, **kwargs):
        super().__init__()
        self.embedding_layer = nn.Linear(FEATURE_DIM, embedding_dim)
        self.layers = nn.ModuleList([EncoderLayer(embedding_dim=embedding_dim, **kwargs) for _ in range(encoder_layer_num)])
    def forward(self, node_features: torch.Tensor, prompt_embedding: torch.Tensor) -> torch.Tensor:
        x = self.embedding_layer(node_features) + prompt_embedding
        for layer in self.layers: x = layer(x)
        return x

class PocatDecoder(nn.Module):
    def __init__(self, embedding_dim: int, head_num: int = 8, **kwargs):
        super().__init__()
        self.embedding_dim = embedding_dim
        self.parent_wq, self.parent_wk = nn.Linear(embedding_dim * 2, embedding_dim, bias=False), nn.Linear(embedding_dim, embedding_dim, bias=False)


class PocatModel(nn.Module):
    def __init__(self, **model_params):
        super().__init__()
        embedding_dim = model_params['embedding_dim']
        self.prompt_net = PocatPromptNet(embedding_dim=embedding_dim)
        self.encoder = PocatEncoder(**model_params)
        self.decoder = PocatDecoder(**model_params)
        self.context_gru = nn.GRUCell(embedding_dim * 2, embedding_dim)
        
        # 💡 새 Load 선택을 위한 별도의 디코더 Query-Key 정의
        self.load_select_wq = nn.Linear(embedding_dim, embedding_dim, bias=False)
        self.load_select_wk = nn.Linear(embedding_dim, embedding_dim, bias=False)

    def forward(self, td: TensorDict, env: PocatEnv):
        prompt_embedding = self.prompt_net(td["prompt_features"])
        encoded_nodes = self.encoder(td["nodes"], prompt_embedding)
        num_starts, start_nodes_idx = env.select_start_nodes(td)
        
        td = batchify(td, num_starts)
        encoded_nodes = batchify(encoded_nodes, num_starts)
        
        batch_size = td.batch_size[0]
        context_embedding = encoded_nodes.mean(dim=1) # 초기 컨텍스트
        log_probs, actions = [], []
        
        # --- 💡 1. Trajectory 기반 디코딩 루프 (while문) ---
        
        # POMO의 첫 스텝: 시작 Load 선택
        # 이 action은 아직 환경에 적용되지 않음 (다음 step에서 사용)
        action = torch.stack([start_nodes_idx, torch.zeros_like(start_nodes_idx)], dim=1)
        td.set("action", action)
        
        output_td = env.step(td)
        td = output_td["next"]
        actions.append(action)
        # 첫 Load 선택은 확률 1로 간주 (로그 확률 0)
        log_probs.append(torch.zeros(batch_size, device=td.device))

        # 메인 디코딩 루프
        while not td["done"].all():
            mask = env.get_action_mask(td)
            phase = td["decoding_phase"][0, 0].item()

            if phase == 0:  # 새 Load 선택
                # 컨텍스트: 메인 트리에 포함된 노드들의 평균 임베딩
                main_tree_nodes = encoded_nodes * td["main_tree_mask"].unsqueeze(-1)
                context_for_load_select = main_tree_nodes.sum(1) / (td["main_tree_mask"].sum(1, keepdim=True) + 1e-9)
                
                # 어텐션을 통해 다음 Load 선택
                q = self.load_select_wq(context_for_load_select).unsqueeze(1)
                k = self.load_select_wk(encoded_nodes)
                scores = torch.matmul(q, k.transpose(1, 2)).squeeze(1) / (self.decoder.embedding_dim ** 0.5)
                scores[~mask[:, :, 0].transpose(0, 1)] = -1e9 # 마스크 형태에 맞게 수정
                
                log_prob = F.log_softmax(scores, dim=-1)
                selected_load = log_prob.argmax(dim=-1)
                action = torch.stack([selected_load, torch.zeros_like(selected_load)], dim=1)
                
                log_prob_val = log_prob.gather(1, selected_load.unsqueeze(-1)).squeeze(-1)

            elif phase == 1:  # Trajectory 구축
                # 컨텍스트: 이전 컨텍스트 + 현재 경로 헤드 정보
                # (GRU 업데이트는 아래 step 이후에 수행)
                trajectory_head_idx = td["trajectory_head"].squeeze(-1)
                head_emb = encoded_nodes[torch.arange(batch_size), trajectory_head_idx]
                
                # 부모 선택 (기존 디코더 로직 활용)
                parent_q_in = torch.cat([context_embedding, head_emb], dim=1)
                parent_q = self.decoder.parent_wq(parent_q_in).unsqueeze(1)
                parent_k = self.decoder.parent_wk(encoded_nodes)
                parent_scores = torch.matmul(parent_q, parent_k.transpose(1, 2)).squeeze(1) / (self.decoder.embedding_dim ** 0.5)
                parent_scores[~mask[torch.arange(batch_size), trajectory_head_idx]] = -1e9
                
                parent_log_probs = F.log_softmax(parent_scores, dim=-1)
                selected_parent_idx = parent_log_probs.argmax(dim=-1)
                
                action = torch.stack([trajectory_head_idx, selected_parent_idx], dim=1)
                log_prob_val = parent_log_probs.gather(1, selected_parent_idx.unsqueeze(-1)).squeeze(-1)

            td.set("action", action)
            output_td = env.step(td)
            td = output_td["next"]
            
            actions.append(action)
            log_probs.append(log_prob_val)
            
            # --- 컨텍스트 업데이트 (GRU) ---
            child_emb = encoded_nodes[torch.arange(batch_size), action[:, 0]]
            parent_emb = encoded_nodes[torch.arange(batch_size), action[:, 1]]
            context_embedding = self.context_gru(torch.cat([child_emb, parent_emb], dim=1), context_embedding)
            
        final_reward = output_td["reward"]

        return {
            "reward": final_reward,
            "log_likelihood": torch.stack(log_probs, 1).sum(1) if log_probs else torch.zeros(batch_size, device=td.device),
            "actions": torch.stack(actions, 1) if actions else torch.empty(batch_size, 0, 2, dtype=torch.long, device=td.device)
        }