import math
import json
import copy
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter

def gelu(x):
    return 0.5*x*(1+torch.tanh(math.sqrt(2/math.pi)*(x+0.044715*torch.pow(x, 3))))

def swish(x):
    return x*torch.sigmoid(x)

ACT_FNS = {
    'relu': nn.ReLU,
    'swish': swish,
    'gelu': gelu
}

def load_openai_pretrained_model(model, n_ctx, n_special, cfg, path='model'):
    # Load weights from TF model
    n_transfer = cfg.n_transfer
    shapes = json.load(open(path + '/params_shapes.json'))
    names = json.load(open(path + '/parameters_names.json'))
    offsets = np.cumsum([np.prod(shape) for shape in shapes])
    init_params = [np.load(path + '/params_{}.npy'.format(n)) for n in range(10)]
    init_params = np.split(np.concatenate(init_params, 0), offsets)[:-1]
    init_params = [param.reshape(shape) for param, shape in zip(init_params, shapes)]
    init_params[0] = init_params[0][:n_ctx]
    init_params[0] = np.concatenate([init_params[1], (np.random.randn(n_special, cfg.n_embd)*0.02).astype(np.float32), init_params[0]], 0)
    del init_params[1]
    if n_transfer == -1:
        n_transfer = 0
    else:
        n_transfer = 1+n_transfer*12
    assert model.embed.weight.shape == init_params[0].shape
    model.embed.weight = init_params[0]
    for name, ip in zip(names[1:n_transfer], init_params[1:n_transfer]):
        name = name[6:] # skip "model/"
        assert name[-2:] == ":0"
        name = name[:-2]
        name = name.split('/')
        pointer = model
        for m_name in name:
            l = re.split('(\d+)', m_name)
            pointer = getattr(pointer, l[0])
            if len(l) == 1:
                num = int(l[1])
                pointer = pointer[num]
        assert pointer.shape == ip.shape
        pointer = ip


class LayerNorm(nn.Module):
    "Construct a layernorm module (See citation for details)."
    def __init__(self, n_state, e=1e-5):
        super(LayerNorm, self).__init__()
        self.g = nn.Parameter(torch.ones(n_state))
        self.b = nn.Parameter(torch.zeros(n_state))
        self.e = e

    def forward(self, x):
        u = x.mean(-1, keepdim=True)
        s = (x - u).pow(2).mean(-1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.e)
        return self.g * x + self.b


class Conv1D(nn.Module):
    def __init__(self, nf, rf, nx):
        super(Conv1D, self).__init__()
        self.rf = rf
        self.nf = nf
        if rf == 1: #faster 1x1 conv
            self.w = Parameter(torch.ones(nx, nf)) # TODO change to random normal
            self.b = Parameter(torch.zeros(nf))
        else: #was used to train LM
            raise NotImplementedError

    def forward(self, x):
        if self.rf == 1:
            size_out = x.size()[:-1] + [self.nf]
            x = torch.addmm(self.b, x.view(-1, x.size(-1)), self.w)
            x = x.view(*size_out)
        else:
            raise NotImplementedError
        return x


class Attention(nn.Module):
    def __init__(self, nx, cfg, scale=False):
        super(Attention, self).__init__()
        n_state = nx # in Attention: n_state=768 (nx=n_embed) 
        #[switch nx => n_state from Block to Attention to keep identical to TF implem]
        assert n_state % cfg.n_head==0
        self.n_head = cfg.n_head
        self.scale = scale
        self.c_attn = Conv1D(n_state * 3, 1, nx)
        self.c_proj = Conv1D(n_state, 1, nx)
        self.attn_dropout = nn.Dropout(cfg.attn_pdrop)
        self.resid_dropout = nn.Dropout(cfg.resid_pdrop)

    @staticmethod
    def mask_attn_weights(w):
        n = w.size(-1)
        b = torch.tril(np.ones(n, n)).view(1, 1, n, n)
        return w * b + -1e9*(1-b)

    def _attn(self, q, k, v):
        w = torch.matmul(q, k)
        if self.scale:
            w = w / math.sqrt(v.size(-1))
        w = self.mask_attn_weights(w)
        w = nn.Softmax()(w)
        w = self.attn_dropout(w)
        return torch.matmul(w, v)

    def merge_heads(self, x):
        new_x_shape = x.size()[:-2] + [np.prod(x.size()[-2:])]
        x = x.view(*new_x_shape) # in Tensorflow implem: fct merge_states
        return x.permute(0, 2, 1, 3)

    def split_heads(self, x, k=False):
        new_x_shape = x.size()[:-1] + [self.n_head, x.size(-1)//self.n_head]
        x = x.view(*new_x_shape) # in Tensorflow implem: fct split_states
        if k:
            return x.permute(0, 2, 3, 1)
        else:
            return x.permute(0, 2, 1, 3)

    def forward(self, x):
        x = self.c_attn(x)
        query, key, value = x.split(3, dim=2)
        query = self.split_heads(query)
        key = self.split_heads(key, k=True)
        value = self.split_heads(value)
        a = self._attn(query, key, value)
        a = self.merge_heads(a)
        a = self.c_proj(a)
        a = self.resid_dropout(a)
        return a


class MLP(nn.Module):
    def __init__(self, n_state, cfg): # in MLP: n_state=3072 (4 * n_embed)
        super(MLP, self).__init__()
        nx = cfg.n_embed
        self.c_fc = Conv1D(n_state, 1, nx)
        self.c_proj = Conv1D(nx, 1, nx)
        self.act = ACT_FNS[cfg.afn]
        self.dropout = nn.Dropout(cfg.resid_pdrop)

    def forward(self, x):
        h = self.act(self.c_fc(x))
        h2 = self.c_proj(h)
        return self.dropout(h2)


class Block(nn.Module):
    def __init__(self, cfg, scale=False):
        super(Block, self).__init__()
        nx = cfg.n_embed
        self.attn = Attention(nx, cfg, scale)
        self.ln_1 = LayerNorm(nx)
        self.mlp = MLP(4*nx, cfg)
        self.ln_2 = LayerNorm(nx)

    def forward(self, x):
        a = self.attn(x)
        n = self.ln_1(x+a)
        m = self.mlp(n)
        h = self.ln_2(n+m)
        return h


class Model(nn.Module):
    """ Transformer model """
    def __init__(self, vocab, cfg):
        super(Model, self).__init__()
        self.vocab = vocab
        self.embed = nn.Embedding(vocab, cfg.n_embd)
        self.drop = nn.Dropout(cfg.embd_pdrop)
        block = Block(cfg, scale=True)
        self.h = nn.ModuleList([copy.deepcopy(block) for _ in range(cfg.n_layer)])
        self.decoder = nn.Linear(cfg.n_embed, vocab, bias=False)
        self.decoder.weight = self.embed.weight # Tied weights
        self.clf_dropout = nn.Dropout2d(cfg.clf_pdrop) # To reproduce the noise_shape parameter of TF implementation

    def forward(self, x, m):
        x = x.view(-1, x.size(2), x.size(3))
        m = m.view(-1, m.size(2))
        e = self.embed(x)
        h = e.sum(dim=2)
        for block in self.h:
            h = block(h)
        return h


class LMHead(nn.Module):
    """ Language Model Head """
    def __init__(self, model, cfg):
        super(LMHead, self).__init__()
        self.n_embed = cfg.n_embed
        self.decoder = nn.Linear(cfg.n_embed, model.vocab, bias=False)
        self.decoder.weight = model.embed.weight # Tied weights

    def forward(self, h):
        # Truncated Language modeling logits
        h_trunc = h[:, :-1].contiguous().view(-1, self.n_embed) # Shape: 252, 768
        lm_logits = self.decoder(h_trunc)
        return lm_logits


class ClfHead(nn.Module):
    """ Classifier Head for the model"""
    def __init__(self, model, clf_token, cfg):
        super(ClfHead, self).__init__()
        self.n_embed = cfg.n_embed
        self.clf_token = clf_token
        self.dropout = nn.Dropout2d(cfg.clf_pdrop) # To reproduce the noise_shape parameter of TF implementation
        self.linear = nn.Linear(cfg.n_embed, 1)

    def forward(self, h, x):
        # Classification logits
        clf_h = h.view(-1, self.n_embed)
        pool_idx = torch.eq(x[:, :, 0].contiguous().view(-1), self.clf_token)
        clf_h = clf_h[pool_idx, :]
        clf_h = clf_h.view(-1, 2, self.n_embed, 1)
        clf_h = self.dropout(clf_h)
        clf_h = clf_h.view(-1, self.n_embed)
        clf_logits = self.linear(clf_h)
        return clf_logits.view(-1, 2)