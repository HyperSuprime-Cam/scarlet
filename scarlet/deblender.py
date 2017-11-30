from __future__ import print_function, division
import numpy as np
from functools import partial

import proxmin
from proxmin.nmf import Steps_AS
from . import transformations
from . import operators

import logging
logger = logging.getLogger("scarlet")

class Source(object):
    def __init__(self, center, size, psf=None, constraints=None, sed=None, morph=None, fix_sed=False, fix_morph=False, shift_center=0.2, prox_sed=None, prox_morph=None):

        if np.isscalar(size):
            size = (size,) * 2
        else:
            size = size
        self.shift_center = shift_center

        # copy sed/morph if present
        self.sed = np.copy(sed) # works even if None
        if sed is not None:
            # to allow multi-component sources, need to have KxB array
            self.sed = self.sed.reshape((self.K, -1))

        self.morph = np.copy(morph)
        if morph is not None:
            # to allow multi-component sources, need to have K x Nx*Ny array
            self.morph = self.morph.reshape((self.K, -1))

        # set up psf and translations matrices
        if psf is None:
            P = None
        else:
            self.P = self._adapt_PSF(psf)
        self._gammaOp = transformations.GammaOp(size, P=P)

        # set center coordinates and translation operators
        # needs to have GammaOp set up first
        self.set_center(center, size=size)

        # set constraints: first projection-style
        self.constraints = constraints
        self._set_sed_prox(prox_sed, fix_sed)
        self._set_morph_prox(prox_morph, fix_morph)
        # ... then ADMM-style constraints (proxs and L matrices)
        self._set_constraints()

    def __len__(self):
        # because SED/morph may not be properly set, need to return default 1
        try:
            return self.sed.shape[0]
        except IndexError:
            try:
                return self.morph.shape[0]
            except IndexError:
                return 1

    @property
    def K(self):
        return self.__len__()

    @property
    def B(self):
        try:
            return self.sed.shape[1]
        except IndexError:
            return 0

    @property
    def Nx(self):
        return self.right-self.left

    @property
    def Ny(self):
        return self.top - self.bottom

    @property
    def shape(self):
        return (self.B, self.Ny, self.Nx)

    @property
    def image(self):
        morph_shape = (self.K, self.Ny, self.Nx)
        return self.morph.reshape(morph_shape) # this *should* be a view

    @property
    def center_int(self):
        from math import floor
        return (int(floor(self.y)), int(floor(self.x)))

    def get_slice_for(self, im_shape):
        # slice so that self.image[k][slice] corresponds to image[self.bb]
        NY, NX = im_shape[1:]

        left = max(0, -self.left)
        bottom = max(0, -self.bottom)
        right = self.Nx - max(0, self.right - NX)
        top = self.Ny - max(0, self.top - NY)
        return (slice(None), slice(bottom, top), slice(left, right))

    def get_model(self, combine=True, Gamma=None):
        if Gamma is None:
            Gamma = self.Gamma
        # model for all components of this source
        if hasattr(Gamma, 'shape'): # single matrix: one for all bands
            model = np.empty((self.K, self.B, self.Ny*self.Nx))
            for k in range(self.K):
                model[k] = np.outer(self.sed[k], Gamma.dot(self.morph[k]))
        else:
            model = np.zeros((self.K, self.B, self.Ny*self.Nx))
            for k in range(self.K):
                for b in range(self.B):
                    model[k,b] += self.sed[k,b] * Gamma[b].dot(self.morph[k])

        # reshape the image into a 2D array
        model = model.reshape(self.K, self.B, self.Ny, self.Nx)
        if combine:
            model = model.sum(axis=0)
        return model

    def set_center(self, center, size=None):
        self.y, self.x = center

        if size is None:
            size = (self.Ny, self.Nx)

        # make cutout of in units of the original image frame (that defines xy)
        # ensure odd pixel number
        y_, x_ = self.center_int
        self.left, self.right = x_ - size[0]//2, x_ + size[0]//2 + 1
        self.bottom, self.top = y_ - size[1]//2, y_ + size[1]//2 + 1

        # since slice wrap around if start or stop are negative, need to sanitize
        # start values (stop always postive)
        self.bb = (slice(None), slice(max(0, self.bottom), self.top), slice(max(0, self.left), self.right))

        dx = self.x - x_
        dy = self.y - y_
        self.Gamma = self._gammaOp(dy,dx)

    def resize(self):
        raise NotImplementedError()

    def init_source(self, img, weights=None):
        # init with SED of the peak pixels
        # TODO: what should we do if peak is saturated?
        B = img.shape[0]
        self.sed = np.empty((1, B))
        y_, x_ = self.center_int
        self.sed[0] = img[:,y_,x_]
        # ensure proper normalization
        self.sed[0] = proxmin.operators.prox_unity_plus(self.sed[0], 0)

        # init with monotonized image in bb
        if self._gammaOp.P is None:
            self.morph = np.zeros((1, self.Ny, self.Nx))
            morph_slice = self.get_slice_for(img.shape)
            # TODO: account for per-band variations
            self.morph[0][morph_slice[1:]] = img[self.bb].sum(axis=0)
            self.morph = self.morph[0].reshape((1, self.Ny*self.Nx))
            # use predefined strict_monotonicity op if present
            if self.constraints is not None and "m" in self.constraints.keys():
                pos = self.prox_morph[0].find(operators.prox_strict_monotonic)
                prox_mono = self.prox_morph[0].operators[pos]
            else:
                shape = (self.Ny, self.Nx)
                thresh = 1./min(shape)
                prox_mono = operators.prox_strict_monotonic(shape, thresh=thresh)
            self.morph[0] = prox_mono(self.morph[0], 0)
        else: # simply central pixel
            self.morph = np.zeros((1, self.Ny*self.Nx))
            tiny = 1e-10
            cx, cy = self.Nx // 2, self.Ny // 2
            self.morph[0, cy*self.Nx+cx] = img[:,y_,x_].sum(axis=0) + tiny

    def get_morph_error(self, weights):
        w = np.zeros(self.shape)
        w[self.get_slice_for(weights.shape)] = weights[self.bb]
        w = w.reshape(self.B, self.Ny*self.Nx)
        # compute direct error propagation assuming only this source SED(s)
        # and the pixel covariances: Sigma_morph = diag((A^T Sigma^-1 A)^-1)
        # CAVEAT: If done on the entire A matrix, degeneracies in the linear
        # solution arise and substantially amplify the error estimate:
        # A = self.sed.T
        # return np.sqrt(np.diagonal(np.linalg.inv(np.dot(np.multiply(self.w.T[:,None,:], A.T), A)), axis1=1, axis2=2))
        # Instead, estimate noise for each component separately:
        # simple multiplication for diagonal pixel covariance matrix
        return [np.dot(a.T, np.multiply(w, a[:,None]))**-0.5 for a in self.sed]

    def get_sed_error(self, weights):
        w = np.zeros(self.shape)
        w[self.get_slice_for(weights.shape)] = weights[self.bb]
        w = w.reshape(self.B, self.Ny*self.Nx)
        # See explanation in get_morph_error
        return [np.dot(s,np.multiply(w.T, s[None,:].T))**-0.5 for s in self.morph]

    def set_morph_sparsity(self, weights):
        if self.constraints is not None and ("l0" in self.constraints.keys() or "l1" in self.constraints.keys()):
            morph_std = np.median(self.get_morph_error(weights), axis=1)
            # Note: don't use hard/soft thresholds with _plus (non-negative) because
            # that is either happening with prox_plus before in the
            # AlternatingProjections or is not indended
            if "l0" in self.constraints.keys():
                morph_std *= self.constraints['l0']
                for k in range(self.K):
                    pos = self.prox_morph[k].find(proxmin.operators.prox_hard)
                    self.prox_morph[k].operators[pos] = partial(proxmin.operators.prox_hard, thresh=morph_std[k])
            elif "l1" in self.constraints.keys():
                # TODO: Is l1 penalty relative to noise meaningful?
                morph_std *= self.constraints['l1']
                for k in range(self.K):
                    pos = self.prox_morph[k].find(proxmin.operators.prox_soft)
                    self.prox_morph[k].operators[pos] = partial(proxmin.operators.prox_soft, thresh=morph_std[k])
            return morph_std
        else:
            return np.zeros(self.K)

    def _set_sed_prox(self, prox_sed, fix_sed):
        if hasattr(fix_sed, '__iter__') and len(fix_sed) == self.K:
            self.fix_sed = fix_sed
        else:
            self.fix_sed = [fix_sed] * self.K

        if prox_sed is None:
            self.prox_sed = [proxmin.operators.prox_unity_plus] * self.K
        else:
            if hasattr(prox_sed, '__iter__') and len(prox_sed) == self.K:
                self.prox_sed = prox_sed
            else:
                self.prox_sed = [prox_sed] * self.K

    def _set_morph_prox(self, prox_morph, fix_morph):

        if hasattr(fix_morph, '__iter__') and len(fix_morph) == self.K:
            self.fix_morph = fix_morph
        else:
            self.fix_morph = [fix_morph] * self.K

        # prox_morph overwrites everything!
        if prox_morph is not None:
            logger.info("prox_morph set from init argument")

            if hasattr(prox_morph, '__iter__') and len(prox_morph) == self.K:
                self.prox_morph = prox_morph
            else:
                self.prox_morph = [prox_morph] * self.K
        else:
            self.prox_morph = [[proxmin.operators.prox_plus],] * self.K
            if self.constraints is not None:

                # Note: don't use hard/soft thresholds with _plus (non-negative) because
                # that is either happening with prox_plus before or is not indended
                if "l0" in self.constraints.keys():
                    if "l1" in self.constraints.keys():
                        # L0 has preference
                        logger.info("l1 penalty ignored in favor of l0 penalty")
                    for k in range(self.K):
                        self.prox_morph[k].append(partial(proxmin.operators.prox_hard, thresh=0))
                elif "l1" in self.constraints.keys():
                    for k in range(self.K):
                        self.prox_morph[k].append(partial(proxmin.operators.prox_soft, thresh=0))

                if "m" in self.constraints.keys():
                    shape = (self.Ny, self.Nx)
                    thresh = 0
                    for k in range(self.K):
                        self.prox_morph[k].append(operators.prox_strict_monotonic(shape, thresh=thresh))

            for k in range(self.K):
                if len(self.prox_morph[k]) > 1:
                    self.prox_morph[k] = proxmin.operators.AlternatingProjections(self.prox_morph[k], repeat=1)
                else:
                    self.prox_morph[k] = self.prox_morph[k][0]

    def _set_constraints(self):
        self.proxs_g = [None, []] # no constraints on A matrix
        self.Ls = [None, []]
        if self.constraints is None:
            self.proxs_g[1] = None
            self.Ls[1] = None
            return

        shape = (self.Ny, self.Nx)
        for c in self.constraints.keys():
            if c == "M":
                # positive gradients
                self.Ls[1].append(transformations.getRadialMonotonicOp(shape, useNearest=self.constraints[c]))
                self.proxs_g[1].append(proxmin.operators.prox_plus)
            elif c == "S":
                # zero deviation of mirrored pixels
                self.Ls[1].append(transformations.getSymmetryOp(shape))
                self.proxs_g[1].append(proxmin.operators.prox_zero)
            elif c == "c":
                useNearest = self.constraints.get("M", False)
                G = transformations.getRadialMonotonicOp(shape, useNearest=useNearest).toarray()
                self.proxs_g[1].append(partial(operators.prox_cone, G=G))
                self.Ls[1].append(None)
            elif (c == "X" or c == "x"): # X gradient
                cx = int(self.Nx)
                self.Ls[1].append(proxmin.transformations.get_gradient_x(shape, cx))
                if c == "X": # all positive
                    self.proxs_g[1].append(proxmin.operators.prox_plus)
                else: # l1 norm for TV_x
                    self.proxs_g[1].append(partial(proxmin.operators.prox_soft, thresh=self.constraints[c]))
            elif (c == "Y" or c == "y"): # Y gradient
                cy = int(self.Ny)
                self.Ls[1].append(proxmin.transformations.get_gradient_y(shape, cy))
                if c == "Y": # all positive
                    self.proxs_g[1].append(proxmin.operators.prox_plus)
                else: # l1 norm for TV_x
                    self.proxs_g[1].append(partial(proxmin.operators.prox_soft, thresh=self.constraints[c]))

    def _adapt_PSF(self, psf):
        # Simpler for likelihood gradients if psf = const across B
        if hasattr(psf, 'shape'): # single matrix
            return transformations.getPSFOp(psf, (self.Ny, self.Nx))

        P = []
        for b in range(len(psf)):
            P.append(transformations.getPSFOp(psf[b], (self.Ny, self.Nx)))
        return P


class Blend(object):
    """The blended scene as interpreted by the deblender.
    """
    def __init__(self, sources):
        assert len(sources)
        # store all source and make search structures
        self._register_sources(sources)

        # collect all proxs_g and Ls: first A, then S
        self._proxs_g = [source.proxs_g[0] for source in self.sources] + [source.proxs_g[1] for source in self.sources]
        self._Ls = [source.Ls[0] for source in self.sources] + [source.Ls[1] for source in self.sources]

        # center update parameters
        self.center_min_dist = 1e-3
        self.center_wait = 10
        self.center_skip = 10

    def source_of(self, k):
        return self._source_of[k]

    def component_of(self, m, l):
        # search for k that has this (m,l), inverse of source_of
        for k in range(self.K):
            if self._source_of[k] == (m,l):
                return k
        raise IndexError

    def __len__(self):
        """Number of distinct sources"""
        return self.M

    @property
    def B(self):
        try:
            return self._img.shape[0]
        except AttributeError:
            return 0

    def fit(self, img, weights=None, sky=None, init_sources=True, update_order=None, e_rel=1e-2, max_iter=200):

        # set data/weights to define objective function gradients
        self.set_data(img, weights=weights, sky=sky, init_sources=init_sources, update_order=update_order, e_rel=e_rel)

        # perform up to max_iter steps
        return self.step(max_iter=max_iter)

    def step(self, max_iter=1):
        # collect all SEDs and morphologies, plus associated errors
        XA = []
        XS = []
        for k in range(self.K):
            m,l = self.source_of(k)
            XA.append(self.sources[m].sed[l])
            XS.append(self.sources[m].morph[l])
        X = XA + XS

        # update_order for bSDMM is over *all* components
        if self.update_order[0] == 0:
            _update_order = range(2*self.K)
        else:
            _update_order = range(self.K,2*self.K) + range(self.K)

        # run bSDMM on all SEDs and morphologies
        steps_g = None
        steps_g_update = 'steps_f'
        traceback = False
        accelerated = True
        res = proxmin.algorithms.bsdmm(X, self._prox_f, self._steps_f, self._proxs_g, steps_g=steps_g, Ls=self._Ls, update_order=_update_order, steps_g_update=steps_g_update, max_iter=max_iter, e_rel=self.e_rel, e_abs=self.e_abs, accelerated=accelerated, traceback=traceback)

        return self

    def set_data(self, img, weights=None, sky=None, init_sources=True, update_order=None, e_rel=1e-2, slack=0.9):
        self.it = 0
        self._model_it = -1

        if sky is None:
            self._ = img
        else:
            self._img = img-sky

        if update_order is None:
            self.update_order = [1,0] # S then A
        else:
            self.update_order = update_order

        self._set_weights(weights)
        WAmax = np.max(self._weights[1])
        WSmax = np.max(self._weights[2])
        self._stepAS = Steps_AS(WAmax=WAmax, WSmax=WSmax, slack=slack, update_order=self.update_order)
        self.step_AS = [None] * 2

        if init_sources:
            self.init_sources()

        # set sparsity cutoff for morph based on the error level
        # TODO: Computation only correct if psf=None!
        self.e_rel = [e_rel] * 2*self.K
        self.e_abs = [e_rel / self.B] * self.K + [0.] * self.K
        if weights is not None:
            for m in range(self.M):
                morph_std = self.sources[m].set_morph_sparsity(weights)
                for l in range(self.sources[m].K):
                    self.e_abs[self.K + self.component_of(m,l)] = e_rel * morph_std[l]

    def init_sources(self):
        for m in range(self.M):
            self.sources[m].init_source(self._img.reshape(self._img.shape), weights=self._weights[0])

    def get_model(self, m=None, combine=True, combine_source_components=True):
        """Compute the current model for the entire image
        """
        _model_img = np.zeros(self._img.shape)
        if m is None:
            model = [source.get_model(combine=combine_source_components) for source in self.sources]
            if combine:
                for m in range(self.M):
                    if len(model[m].shape) == 4: # several components in model
                        model[m] = model[m].sum(axis=0)
                    _model_img[self.sources[m].bb] += model[m][self.sources[m].get_slice_for(self._img.shape)]
                model_img = _model_img
            else:
                model_img = []
                for m in range(self.M):
                    model_slice = self.sources[m].get_slice_for(self._img.shape)
                    if len(model[m].shape) == 4: # several components in model
                        for k in range(model[m].shape[0]):
                            model_img.append(_model_img.copy())
                            model_img[-1][self.sources[m].bb] = model[m][k][model_slice]
                    else:
                        model_img.append(_model_img.copy())
                        model_img[-1][self.sources[m].bb] = model[m][model_slice]
        else:
            model = self.sources[m].get_model(combine=combine)
            model_slice = self.sources[m].get_slice_for(self._img.shape)
            if len(model.shape) == 4: # several components in model
                model_img = [_model_img.copy() for k in range(model.shape[0])]
                for k in range(model.shape[0]):
                    model_img[k][self.sources[m].bb] = model[k][model_slice]
            else:
                _model_img[self.sources[m].bb] = model[model_slice]
                model_img = _model_img
        return np.array(model_img)

    def _register_sources(self, sources):
        self.sources = sources # do not copy!
        self.M = len(self.sources)
        self.K =  sum([source.K for source in self.sources])
        self.psf_per_band = not hasattr(sources[0].Gamma, 'shape')

        # lookup of source/component tuple given component number k
        self._source_of = []
        self.update_centers = False
        for m in range(self.M):
            self.update_centers |= bool(self.sources[m].shift_center)
            for l in range(self.sources[m].K):
                self._source_of.append((m,l))

    def _set_weights(self, weights):
        if weights is None:
            self._weights = [1,1,1]
        else:
            self._weights = [weights, None, None] # [W, WA, WS]

            # for S update: normalize the per-pixel variation
            # i.e. in every pixel: utilize the bands with large weights
            # CAVEAT: need to filter out pixels that are saturated in every band
            norm_pixel = np.median(weights, axis=0)
            mask = norm_pixel > 0
            self._weights[2] = weights.copy()
            self._weights[2][:,mask] /= norm_pixel[mask]

            # reverse is true for A update: for each band, use the pixels that
            # have the largest weights
            norm_band = np.median(weights, axis=(1,2))
            # CAVEAT: some regions may have one band missing completely
            mask = norm_band > 0
            self._weights[1] = weights.copy()
            self._weights[1][mask] /= norm_band[mask,None,None]
            # CAVEAT: mask all pixels in which at least one band has W=0
            # these are likely saturated and their colors have large weights
            # but are incorrect due to missing bands
            mask = ~np.all(weights>0, axis=0)
            # and mask all bands for that pixel:
            # when estimating A do not use (partially) saturated pixels
            self._weights[1][:,mask] = 0

    def _compute_model(self):
        # make sure model at current iteration is computed when needed
        # irrespective of function that needs it
        if self._model_it < self.it:
            self._models = self.get_model(combine=False, combine_source_components=False) # model each each component over image
            self._model = np.sum(self._models, axis=0)
            self._model_it = self.it

    def _prox_f(self, X, step, Xs=None, j=None):

        # which update to do now
        AorS = j//self.K
        k = j%self.K

        # computing likelihood gradients for S and A:
        # build model only once per iteration
        if k == 0:
            if AorS == self.update_order[0]:
                self._compute_model()

                # update positions?
                if self.update_centers:
                    if self.it >= self.center_wait and self.it % self.center_skip == 0:
                        self._update_positions()
                self.it += 1

            # compute weighted residuals
            self._diff = self._weights[AorS + 1]*(self._model-self._img)

        # A update
        if AorS == 0:
            m,l = self.source_of(k)
            if not self.sources[m].fix_sed[l]:
                # gradient of likelihood wrt A: nominally np.dot(diff, S^T)
                # but with PSF convolution, S_ij -> sum_q Gamma_bqi S_qj
                # however, that's exactly the operation done for models[k]
                # caveat: the model is SED * convolved model -> need to divide
                grad = np.einsum('...ij,...ij', self._diff, self._models[k] / self.sources[m].sed[l].T[:,None,None])

                # apply per component prox projection and save in source
                self.sources[m].sed[l] =  self.sources[m].prox_sed[l](X - step*grad, step)
            return self.sources[m].sed[l]

        # S update
        elif AorS == 1:
            m,l = self.source_of(k)
            if not self.sources[m].fix_morph[l]:
                # gradient of likelihood wrt S: nominally np.dot(A^T,diff)
                # but again: with convolution, it's more complicated

                # first create diff image in frame of source k
                diff_k = np.zeros(self.sources[k].shape)
                diff_k[self.sources[k].get_slice_for(self._img.shape)] = self._diff[self.sources[k].bb]

                grad = np.zeros_like(X)
                if not self.psf_per_band:
                    for b in range(self.B):
                        grad += self.sources[m].sed[l,b]*self.sources[m].Gamma.T.dot(diff_k[b].flatten())
                else:
                    for b in range(self.B):
                        grad += self.sources[m].sed[l,b]*self.sources[m].Gamma[b].T.dot(diff_k[b].flatten())

                # apply per component prox projection and save in source
                self.sources[m].morph[l] = self.sources[m].prox_morph[l](X - step*grad, step)
            return self.sources[m].morph[l]
        else:
            raise ValueError("Expected index j in [0,%d]" % (2*self.K))

    def _get_shift_differential(self, m):
        # compute (model - dxy*shifted_model)/dxy for first-order derivative
        source = self.sources[m]
        slice_m = source.get_slice_for(self._img.shape)
        k = self.component_of(m, 0)
        model_m = self._models[k][self.sources[m].bb]
        # in self._models, per-source components aren't combined,
        # need to combine here
        for k in range(1,source.K):
            model_m += self._models[k][self.sources[m].bb]

        # get Gamma matrices of source m with additional shift
        offset = source.shift_center
        y_, x_ = source.center_int
        dx = source.x - x_
        dy = source.y - y_
        dGamma_x = source._gammaOp(dy, dx+offset)
        dGamma_y = source._gammaOp(dy + offset, dx)
        diff_img = [source.get_model(combine=True, Gamma=dGamma_x), source.get_model(combine=True, Gamma=dGamma_y)]
        diff_img[0] = (model_m-diff_img[0][slice_m])/source.shift_center
        diff_img[1] = (model_m-diff_img[1][slice_m])/source.shift_center
        return diff_img

    def _update_positions(self):
        # residuals weighted with full/original weight matrix
        y = self._weights[0]*(self._model-self._img)
        for m in range(self.M):
            if self.sources[m].shift_center:
                source = self.sources[m]
                bb_m = source.bb
                diff_x,diff_y = self._get_shift_differential(m)
                diff_x[:,:,-1] = 0
                diff_y[:,-1,:] = 0
                # least squares for the shifts given the model residuals
                MT = np.vstack([diff_x.flatten(), diff_y.flatten()])
                if not hasattr(self._weights[0],'shape'): # no/flat weights
                    ddx,ddy = np.dot(np.dot(np.linalg.inv(np.dot(MT, MT.T)), MT), y[bb_m].flatten())
                else:
                    w = self._weights[0][bb_m].flatten()[:,None]
                    ddx,ddy = np.dot(np.dot(np.linalg.inv(np.dot(MT, MT.T*w)), MT), y[bb_m].flatten())
                if ddx**2 + ddy**2 > self.center_min_dist**2:
                    center = (source.y + ddy, source.x + ddx)
                    source.set_center(center)
                    logger.info("Source %d shifted by (%.3f/%.3f) to (%.3f/%.3f)" % (m, ddx, ddy, source.x, source.y))

    def _steps_f(self, j, Xs):

        # which update to do now
        AorS = j//self.K
        k = j%self.K

        # computing likelihood gradients for S and A: only once per iteration
        if AorS == self.update_order[0] and k==0:
            self._compute_model()

            # build temporary A,S matrices
            B, Ny, Nx = self._img.shape
            A = np.empty((self.B,self.K))
            for k_ in range(self.K):
                m,l = self._source_of[k_]
                A[:,k_] = self.sources[m].sed[l]
            if not self.psf_per_band:
                # model[b] is simple SED[b] * S, need to divide by SED
                b = 0
                S = self._models[:,b,:,:].reshape((self.K, Ny*Nx)) / A.T[:,b][:,None]
            else:
                # TODO: replace this with the current models, for each band
                # i.e. one S per band -> step_size per band
                raise NotImplementedError
            self.step_AS[0] = self._stepAS(0, [A, S])
            self.step_AS[1] = self._stepAS(1, [A, S])
        return self.step_AS[AorS]
