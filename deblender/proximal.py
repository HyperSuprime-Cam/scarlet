from __future__ import print_function, division
import logging
from functools import partial

import numpy as np

from . import operators
from . import proximal_utils

def prox_monotonic(X, step, ref_idx, dist_idx, thresh=0, prox_chain=None, **kwargs):
    """Force an intensity profile to be monotonic
    """
    # It may be necessary to chain proximal operators together,
    # so call proximal operators lower in the chain
    if prox_chain is not None:
        X = prox_chain(X, step, **kwargs)

    proximal_utils.prox_monotonic(X, step, ref_idx, dist_idx, thresh)
    return X

def build_prox_monotonic(shape, prox_chain=None, thresh=0):
    """Build the prox_monotonic operator
    """
    monotonicOp = operators.getRadialMonotonicOp(shape)
    _, refIdx = np.where(monotonicOp.toarray()==1)
    # Get the center pixels
    px = (shape[1]-1) >> 1
    py = (shape[0]-1) >> 1
    # Calculate the distance between each pixel and the peak
    x = np.arange(shape[1])
    y = np.arange(shape[0])
    X,Y = np.meshgrid(x,y)
    X = X - px
    Y = Y - py
    distance = np.sqrt(X**2+Y**2)
    # Get the indices of the pixels sorted by distance from the peak
    didx = np.argsort(distance.flatten())
    #update the strict proximal operators
    return partial(prox_monotonic, ref_idx=refIdx.tolist(), dist_idx=didx.tolist(), prox_chain=prox_chain, thresh=thresh)