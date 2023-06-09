import math
import numpy as np
import scipy as sp
import scipy.linalg
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
from nf.utils import unconstrained_RQS
from torch.distributions import HalfNormal, Weibull, Gumbel
if torch.cuda.is_available():    
    device = torch.device("cuda")
elif torch.backends.mps.is_available():
    device = torch.device("mps")
else:
    device = torch.device("cpu")


class FCNN(nn.Module):
    """
    Simple fully connected neural network.
    """

    def __init__(self, in_dim, out_dim, hidden_dim, activation="tanh"):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(in_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, out_dim),
            )

    def forward(self, x):
        return self.network(x)




class NSF_Reg_CNNcond(nn.Module):
    """
    This function models the probability of observing all the lower halo masses
    """

    def __init__(
        self,
        dim=None,
        K=5,
        B=3,
        hidden_dim=8,
        base_network=FCNN,
        num_cond=0,
        nflows=1,
        ngauss=1,
        base_dist="gumbel",
        mu_pos=False,
        ):
        super().__init__()
        self.dim = dim
        self.K = K
        self.B = B
        self.num_cond = num_cond
        self.nflows = nflows
        self.ngauss = ngauss
        self.base_dist = base_dist
        self.mu_pos = mu_pos
        self.num_cond = num_cond
        self.init_param = nn.Parameter(torch.Tensor(3 * K - 1))
        self.layers_all_dim = nn.ModuleList()
        self.layers_all_dim_init = nn.ModuleList()

        for jd in range(dim):
            if self.base_dist in ["gauss", "halfgauss"]:
                if self.ngauss == 1:
                    layer_init_gauss = base_network(self.num_cond + jd, 2, hidden_dim)
                else:
                    layer_init_gauss = base_network(self.num_cond + jd, 3 * self.ngauss, hidden_dim)
            elif self.base_dist == 'weibull':
                layer_init_gauss = base_network(self.num_cond + jd, 2, hidden_dim)
            elif self.base_dist == 'gumbel':
                layer_init_gauss = base_network(self.num_cond + jd, 2, hidden_dim)
            else:
                print('base_dist not recognized')
                raise ValueError
            self.layers_all_dim_init += [layer_init_gauss]

            layers = nn.ModuleList()
            for jf in range(nflows):
                layers += [base_network(self.num_cond + jd, 3 * K - 1, hidden_dim)]
            self.layers_all_dim += [layers]

        self.reset_parameters()

    def reset_parameters(self):
        init.uniform_(self.init_param, -1 / 2, 1 / 2)

    def get_gauss_func_mu_alpha(self, jd, cond_inp=None):
        out = self.layers_all_dim_init[jd](cond_inp)
        if self.ngauss == 1:
            mu, alpha = out[:, 0], out[:, 1]
            if self.mu_pos:
                mu = (1 + nn.Tanh()(mu)) / 2
            var = torch.exp(alpha)
            return mu, var
        else:
            mu_all, alpha_all, pw_all = (
                out[:, 0:self.ngauss], out[:, self.ngauss:2 * self.ngauss], out[:, 2 * self.ngauss:3 * self.ngauss]
                )
            if self.mu_pos:
                mu_all = (1 + nn.Tanh()(mu_all)) / 2
            pw_all = nn.Softmax(dim=1)(pw_all)
            var_all = torch.exp(alpha_all)
            return mu_all, var_all, pw_all

    def forward(self, x_inp, cond_inp=None, mask=None):
        logp = torch.zeros_like(x_inp)
        logp = logp.to(device)
        x_inp = x_inp.to(device)
        for jd in range(self.dim):
            # print(cond_inp.shape)
            if jd > 0:
                cond_inp_jd = torch.cat([cond_inp, x_inp[:, :jd]], dim=1)
            else:
                cond_inp_jd = cond_inp
            # print(cond_inp.shape)
            if self.base_dist in ["halfgauss"]:
                if self.ngauss == 1:
                    mu, var = self.get_gauss_func_mu_alpha(jd, cond_inp_jd)
            elif self.base_dist in ['weibull', 'gumbel']:
                out = self.layers_all_dim_init[jd](cond_inp_jd)
                mu, alpha = out[:, 0], out[:, 1]
                if self.base_dist == 'weibull':
                    scale, conc = torch.exp(mu), torch.exp(alpha)
                else:
                    if self.mu_pos:
                        # mu = torch.exp(mu)
                        mu = ((1 + nn.Tanh()(mu)) / 2)*self.B
                    sig = torch.exp(alpha)
            else:
                print('base_dist not recognized')
                raise ValueError

            # if len(x.shape) > 1:
            #     x = x[:, 0]
            log_det_all_jd = torch.zeros(x_inp.shape[0])
            log_det_all_jd = log_det_all_jd.to(device)
            for jf in range(self.nflows):
                if jf == 0:
                    x = x_inp[:, jd]
                    x = x.to(device)
                out = self.layers_all_dim[jd][jf](cond_inp_jd)
                # z = torch.zeros_like(x)
                # log_det_all = torch.zeros(z.shape)
                W, H, D = torch.split(out, self.K, dim=1)
                W, H = torch.softmax(W, dim=1), torch.softmax(H, dim=1)
                W, H = 2 * self.B * W, 2 * self.B * H
                D = F.softplus(D)
                z, ld = unconstrained_RQS(x, W, H, D, inverse=False, tail_bound=self.B)
                log_det_all_jd += ld
                x = z

            if self.base_dist == 'halfgauss':
                if self.ngauss == 1:
                    x = torch.exp(x - mu)
                    hf = HalfNormal((torch.sqrt(var)))
                    logp_jd = hf.log_prob(x)

            elif self.base_dist == 'weibull':
                hf = Weibull(scale, conc)
                logp_jd = hf.log_prob(x)
                # if there are any nans of infs, replace with -100
                logp_jd[torch.isnan(logp_jd) | torch.isinf(logp_jd)] = -100
            elif self.base_dist == 'gumbel':
                hf = Gumbel(mu, sig)
                logp_jd = hf.log_prob(x)
                logp_jd[torch.isnan(logp_jd) | torch.isinf(logp_jd)] = -100
            else:
                raise ValueError("Base distribution not supported")

            logp[:, jd] = log_det_all_jd + logp_jd
        logp *= mask
        logp = torch.sum(logp, dim=1)
        # print(logp.shape, mask.shape)
        return logp

    def inverse(self, cond_inp=None, mask=None):
        z_out = torch.zeros((cond_inp.shape[0], self.dim))
        z_out = z_out.to(device)
        for jd in range(self.dim):
            if jd > 0:
                cond_inp_jd = torch.cat([cond_inp, z_out[:, :jd]], dim=1)
            else:
                cond_inp_jd = cond_inp
            if self.base_dist in ["halfgauss"]:
                if self.ngauss == 1:
                    mu, var = self.get_gauss_func_mu_alpha(jd, cond_inp_jd)

            elif self.base_dist in ['gumbel', 'weibull']:
                out = self.layers_all_dim_init[jd](cond_inp_jd)
                mu, alpha = out[:, 0], out[:, 1]
                if self.base_dist == 'weibull':
                    scale, conc = torch.exp(mu), torch.exp(alpha)
                else:
                    if self.mu_pos:
                        # mu = torch.exp(mu)
                        # mu = (1 + nn.Tanh()(mu)) / 2
                        mu = ((1 + nn.Tanh()(mu)) / 2)*self.B
                    sig = torch.exp(alpha)
            else:
                print('base_dist not recognized')
                raise ValueError

            if self.base_dist == 'gauss':
                if self.ngauss == 1:
                    x = mu + torch.randn(cond_inp_jd.shape[0], device=device) * torch.sqrt(var)

            elif self.base_dist == 'halfgauss':
                if self.ngauss == 1:
                    x = torch.log(mu + torch.abs(torch.randn(cond_inp_jd.shape[0], device=device)) * torch.sqrt(var))

            elif self.base_dist == 'weibull':
                hf = Weibull(scale, conc)
                x = hf.sample()
            elif self.base_dist == 'gumbel':
                hf = Gumbel(mu, sig)
                x = hf.sample()
                # print(x.shape)
                # print(mu, sig)
            else:
                raise ValueError("Base distribution not supported")

            log_det_all = torch.zeros_like(x)
            for jf in range(self.nflows):
                ji = self.nflows - jf - 1
                out = self.layers_all_dim[jd][ji](cond_inp_jd)
                z = torch.zeros_like(x)
                W, H, D = torch.split(out, self.K, dim=1)
                W, H = torch.softmax(W, dim=1), torch.softmax(H, dim=1)
                W, H = 2 * self.B * W, 2 * self.B * H
                D = F.softplus(D)
                z, ld = unconstrained_RQS(x, W, H, D, inverse=True, tail_bound=self.B)
                log_det_all += ld
                x = z

            x *= mask[:, jd]
            z_out[:, jd] = x
        return z_out, log_det_all

    def sample(self, cond_inp=None, mask=None):
        x, _ = self.inverse(cond_inp, mask)
        return x


class MAF_CNN_cond(nn.Module):
    """
    This is the model for the auto-regressive model of the lower halo masses.
    This takes as input the environment, heavist halo mass and number of halos. 
    It is based on simple CNNs with auto-regressive structure.
    """

    def __init__(self, dim, hidden_dim=8, base_network=FCNN, num_cond=0):
        super().__init__()
        self.dim = dim
        self.layers = nn.ModuleList()
        self.num_cond = num_cond
        if self.num_cond == 0:
            self.initial_param = nn.Parameter(torch.Tensor(1))
        else:
            self.layer_init = base_network(self.num_cond, 2, hidden_dim)
        for i in range(1, dim):
            self.layers += [base_network(self.num_cond + i, 2, hidden_dim)]
        self.mu_all_forward = np.zeros(self.dim)
        self.alpha_all_forward = np.zeros(self.dim)
        self.mu_all_inverse = torch.zeros(self.dim)
        self.alpha_all_inverse = torch.zeros(self.dim)
        if self.num_cond == 0:
            self.reset_parameters()

    def reset_parameters(self):
        init.uniform_(self.initial_param, -math.sqrt(0.5), math.sqrt(0.5))

    def forward(self, x, cond_inp=None, mask=None):
        z = torch.zeros_like(x)
        # log_det = torch.zeros(z.shape[0])
        log_det_all = torch.zeros_like(x)

        for i in range(self.dim):
            if i == 0:
                out = self.layer_init(cond_inp)
                mu, alpha = out[:, 0], out[:, 1]
                mu = -torch.exp(mu)
                # mu = (1 + nn.Tanh()(mu))
            else:
                out = self.layers[i - 1](torch.cat([cond_inp, x[:, :i]], dim=1))
                mu, alpha = out[:, 0], out[:, 1]
                # mu = -torch.exp(mu)
                mu = (1 + nn.Tanh()(mu))

            z[:, i] = (x[:, i] - mu) / torch.exp(alpha)
            log_det_all[:, i] = -alpha

        log_det_all_masked = log_det_all * mask
        log_det = torch.sum(log_det_all_masked, dim=1)
        return z, log_det

    def inverse(self, z, cond_inp=None, mask=None):
        x = torch.zeros_like(z)
        x = x.to(device)
        z = z.to(device)
        log_det_all = torch.zeros_like(z)
        log_det_all = log_det_all.to(device)
        for i in range(self.dim):
            if i == 0:
                out = self.layer_init(cond_inp)
                mu, alpha = out[:, 0], out[:, 1]
                mu = -torch.exp(mu)
                # mu = (1 + nn.Tanh()(mu))
            else:
                out = self.layers[i - 1](torch.cat([cond_inp, x[:, :i]], dim=1))
                mu, alpha = out[:, 0], out[:, 1]
                # mu = -torch.exp(mu)
                mu = (1 + nn.Tanh()(mu))

            x[:, i] = mu + torch.exp(alpha) * z[:, i]
            log_det_all[:, i] = alpha

        log_det_all_masked = log_det_all * mask
        log_det = torch.sum(log_det_all_masked, dim=1)
        x *= mask
        return x, log_det
