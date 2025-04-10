import os
import sys
import time
import math
import argparse
from dataclasses import dataclass
from typing import List

import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.utils.data import Dataset
from torch.utils.data.dataloader import DataLoader
from torch.utils.tensorboard import SummaryWriter

# -----------------------------------------------------------------------------
# Model Configuration
@dataclass
class ModelConfig:
    block_size: int = None  # Length of the input sequences of integers
    vocab_size: int = None  # The input integers are in range [0 .. vocab_size -1]
    n_layer: int = 4        # Number of transformer layers
    n_embd: int = 64        # Embedding dimension
    n_embd2: int = 64       # Secondary embedding size (not used explicitly)
    n_head: int = 4         # Number of attention heads

# -----------------------------------------------------------------------------
# Transformer Language Model (*exactly* as used in GPT-2)

class NewGELU(nn.Module):
    """
    Implementation of the GELU activation function as used in Google BERT and OpenAI GPT.
    Reference: Gaussian Error Linear Units (GELU) paper: https://arxiv.org/abs/1606.08415
    """
    def forward(self, x):
        return 0.5 * x * (1.0 + torch.tanh(math.sqrt(2.0 / math.pi) * (x + 0.044715 * torch.pow(x, 3.0))))

class CausalSelfAttention(nn.Module):
    """
    A vanilla multi-head masked self-attention layer with a projection at the end.
    This ensures that the model attends only to previous tokens in the sequence.
    """
    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0  # Ensure that embedding dimension is divisible by the number of heads
        
        # Linear layers to project input embeddings into query (Q), key (K), and value (V)
        self.c_attn = nn.Linear(config.n_embd, 3 * config.n_embd)
        self.c_proj = nn.Linear(config.n_embd, config.n_embd)  # Output projection
        
        # Causal mask ensures the model cannot look at future tokens
        self.register_buffer("bias", torch.tril(torch.ones(config.block_size, config.block_size))
                                     .view(1, 1, config.block_size, config.block_size))
        
        self.n_head = config.n_head
        self.n_embd = config.n_embd

    def forward(self, x):
        B, T, C = x.size()  # Batch size (B), Sequence length (T), Embedding size (C)
        
        # Compute Q, K, V for all heads in batch
        q, k, v = self.c_attn(x).split(self.n_embd, dim=2)
        
        # Reshape Q, K, V to separate attention heads
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)
        
        # Compute attention scores (scaled dot-product attention)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.bias[:, :, :T, :T] == 0, float('-inf'))  # Apply causal mask
        att = F.softmax(att, dim=-1)
        
        # Apply attention to values (V)
        y = att @ v
        
        # Reshape back to original shape
        y = y.transpose(1, 2).contiguous().view(B, T, C)
        
        # Output projection
        y = self.c_proj(y)
        return y

class Block(nn.Module):
    """ A single Transformer block consisting of self-attention and an MLP """
    def __init__(self, config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)  # Layer normalization before attention
        self.attn = CausalSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embd)  # Layer normalization before MLP
        
        # MLP with GELU activation
        self.mlp = nn.ModuleDict(dict(
            c_fc    = nn.Linear(config.n_embd, 4 * config.n_embd),
            c_proj  = nn.Linear(4 * config.n_embd, config.n_embd),
            act     = NewGELU(),
        ))
        
        self.mlpf = lambda x: self.mlp.c_proj(self.mlp.act(self.mlp.c_fc(x)))

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))  # Add & Norm before self-attention
        x = x + self.mlpf(self.ln_2(x))  # Add & Norm before MLP
        return x

class Transformer(nn.Module):
    """ Transformer Language Model, similar to GPT-2 """
    def __init__(self, config):
        super().__init__()
        self.block_size = config.block_size  # Maximum sequence length
        
        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size, config.n_embd),  # Token embeddings
            wpe = nn.Embedding(config.block_size, config.n_embd),  # Positional embeddings
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),  # Transformer blocks
            ln_f = nn.LayerNorm(config.n_embd),  # Final layer norm
        ))
        
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)  # Final output layer
        
        # Print the number of parameters
        n_params = sum(p.numel() for p in self.transformer.parameters())
        print("Number of parameters: %.2fM" % (n_params / 1e6,))
    
    def get_block_size(self):
        return self.block_size
    
    def forward(self, idx, targets=None):
        device = idx.device
        b, t = idx.size()
        assert t <= self.block_size, f"Cannot forward sequence of length {t}, block size is only {self.block_size}"
        
        pos = torch.arange(0, t, dtype=torch.long, device=device).unsqueeze(0)  # Position indices
        
        # Compute token and positional embeddings
        tok_emb = self.transformer.wte(idx)
        pos_emb = self.transformer.wpe(pos)
        x = tok_emb + pos_emb
        
        # Pass through Transformer blocks
        for block in self.transformer.h:
            x = block(x)
        
        x = self.transformer.ln_f(x)  # Final layer norm
        logits = self.lm_head(x)  # Compute logits
        
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)  # Compute loss
        
        return logits, loss