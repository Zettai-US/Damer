"""Deterministic scientific kernels and a multi-output polynomial surrogate.

These small PDEs are controlled scientific workloads, not production discovery
campaigns. All policies execute exactly the same numerical algorithm.
"""
import numpy as np


def laplace(x):
    return (np.roll(x, 1, 0) + np.roll(x, -1, 0) +
            np.roll(x, 1, 1) + np.roll(x, -1, 1) - 4 * x)


def initial(n, kind):
    q = (np.arange(n, dtype=np.float32) - n / 2) / n
    x, y = np.meshgrid(q, q)
    if kind == 'gray-scott':
        u = np.ones((n, n), dtype=np.float32)
        v = np.zeros_like(u)
        mask = (abs(x) < .12) & (abs(y) < .12)
        u[mask] = .5
        v[mask] = .25 + .01 * np.cos(60*x[mask]) * np.sin(60*y[mask])
    elif kind == 'wave':
        u = np.exp(-100*(x*x+y*y)).astype(np.float32)
        v = np.zeros_like(u)
    else:
        raise ValueError(kind)
    return u, v


def simulate(kind, params, n, steps, frames, out):
    """Periodic unit-spacing grids; dt=1 (Gray--Scott), dt=.2 (wave).

    Gray--Scott uses Du=.16, Dv=.08; F=.02+.04*a, k=.045+.025*b.
    Wave uses semi-implicit Euler: v'=c^2 lap(u)-gamma*v; u'=v,
    c=.5+a, gamma=.01+.09*b. Its maximum Courant number is .3.
    """
    if steps % frames:
        raise ValueError('steps must be divisible by frames')
    a, b = params
    u, v = initial(n, kind)
    interval = steps // frames
    for step in range(steps):
        if kind == 'gray-scott':
            uvv = u * v * v
            du = .16 * laplace(u) - uvv + (.02+.04*a)*(1-u)
            dv = .08 * laplace(v) + uvv - (.065+.04*a+.025*b)*v
            u += du
            v += dv
        else:
            v += .2 * ((.5+a)**2 * laplace(u) - (.01+.09*b)*v)
            u += .2 * v
        if (step+1) % interval == 0:
            out[(step+1)//interval-1, 0] = u
            out[(step+1)//interval-1, 1] = v
    if not np.isfinite(out).all():
        raise FloatingPointError('non-finite simulation')


def basis(params, t):
    a, b = np.asarray(params)*2-1
    t = np.asarray(t)*2-1
    a, b, t = np.broadcast_arrays(a, b, t)
    return np.stack([np.ones_like(t), a, b, t, a*a, b*b, t*t,
                     a*b, a*t, b*t], axis=-1).astype(np.float32)


def candidates(seed):
    return np.random.default_rng(seed).uniform(0, 1, (64, 2))


def training_contribution(data, params, weight):
    x = basis(params, np.linspace(1/data.shape[0], 1, data.shape[0]))
    # One BLAS thread per worker; bulk input is scanned directly in its tier.
    return weight*(x.T @ x), weight*(x.T @ data.reshape(data.shape[0], -1))


def fit(gram, cross, n):
    return np.linalg.solve(gram + .05*np.eye(10, dtype=np.float32), cross).reshape(10, 2, n, n)
