import autograd.numpy as np
from scipy import fftpack

from . import interpolation

from . import resampling

import logging

logger = logging.getLogger("scarlet.observation")

def _centered(arr, newshape):
    """Return the center newshape portion of the array.

    This function is used by `fft_convolve` to remove
    the zero padded region of the convolution.
    """
    newshape = np.asarray(newshape)
    currshape = np.array(arr.shape)
    startind = (currshape - newshape) // 2
    endind = startind + newshape
    myslice = [slice(startind[k], endind[k]) for k in range(len(endind))]

    return arr[tuple(myslice)]

def convolve_1Dfft(img, ker, ker_fft = None, axis = 0, return_fft = False):
    '''Performs 1D convolutions between 2D interpolation kernels and images along a specified axis.

    Parameters
    ----------
    img: array
        image to convolve (here actually image to interpolate)
    ker: array
        a set of 1D interpolation kernels
    axis: int
        axis along which the 1D convolutions are performed
    ker_fft: array
        if provided, ker_fft is used as the fft of the sinc kernel. This is intended to avoid performing multiple
        ffts on the same kernel
    Returns
    -------
    result: array
        vector for convolution and resampling of the high resolution plane into pixel (xm,ym) at low resolution
    '''
    #Records original size of convolution axis
    if axis == 0:
        _shape = [ker.shape[0],ker.shape[1],img.shape[1]]
    elif axis == 1:
        _shape = [img.shape[0],ker.shape[0], ker.shape[1]]

    ker_axis = np.abs(axis-1)
    #finds fast sizes for numpy's fft
    fast_shape = fftpack.helper.next_fast_len(np.max([ker.shape[ker_axis], img.shape[axis]]))

    #performs fft of convolution actors
    img_fft = np.fft.rfftn(img, [fast_shape], axes = [axis])

    if ker_fft is None:
        ker_fft = np.fft.rfftn(ker, [fast_shape], axes = [ker_axis])

    # Convolution in Fourier domain
    if axis == 0:
        res_fft = img_fft[np.newaxis,:,:]*ker_fft[:,:,np.newaxis]

    elif axis == 1:
        res_fft = img_fft[:,:, np.newaxis] * ker_fft[np.newaxis,:,:]

    res = np.fft.ifftshift(np.fft.irfftn(res_fft, axes = [1]), axes = 1)

    if return_fft is True:
        # Unpadding
        return _centered(np.real(res), _shape), ker_fft
    else:
        #Unpadding
        return _centered(np.real(res), _shape)




class Frame():
    """Spatial and spectral characteristics of the data

    Attributes
    ----------
    shape: tuple
        (channels, Ny, Nx) shape of the model image
    wcs: TBD
        World Coordinates
    psfs: array or tensor
        PSF in each band
    channels: list of hashable elements
        Names/identifiers of spectral channels
    dtype: `numpy.dtype`
        Dtype to represent the data.
    """
    def __init__(self, shape, wcs=None, psfs=None, channels=None, dtype=np.float32):
        assert len(shape) == 3
        self._shape = tuple(shape)
        self.wcs = wcs

        if psfs is None:
            logger.warning('No PSFs specified. Possible, but dangerous!')
        else:
            assert len(psfs) == 1 or len(psfs) == shape[0], 'PSFs need to have shape (1,Ny,Nx) for Blend and (B,Ny,Nx) for Observation'
            if not np.allclose(psfs.sum(axis=(1, 2)), 1):
                logger.warning('PSFs not normalized. Normalizing now..')
                psfs /= psfs.sum(axis=(1, 2))[:, None, None]

            if dtype != psfs.dtype:
                msg = "Dtypes of PSFs and Frame different. Casting PSFs to {}".format(dtype)
                logger.warning(msg)
                psfs = psfs.astype(dtype)

        self._psfs = psfs

        assert channels is None or len(channels) == shape[0]
        self.channels = channels
        self.dtype = dtype

    @property
    def C(self):
        """Number of channels in the model
        """
        return self._shape[0]

    @property
    def Ny(self):
        """Number of pixel in the y-direction
        """
        return self._shape[1]

    @property
    def Nx(self):
        """Number of pixels in the x-direction
        """
        return self._shape[2]

    @property
    def shape(self):
        """Shape of the model.
        """
        return self._shape

    @property
    def psfs(self):
        return self._psfs

    def get_pixel(self, sky_coord):
        """Get the pixel coordinate from a world coordinate
        If there is no WCS associated with the `Scene`,
        meaning the data frame and model frame are the same,
        then this just returns the `sky_coord`
        """
        if self.wcs is not None:
            if self.wcs.naxis == 3:
                coord = self.wcs.wcs_world2pix(sky_coord[0], sky_coord[1], 0, 0)
            elif self.wcs.naxis == 2:
                coord = self.wcs.wcs_world2pix(sky_coord[0], sky_coord[1], 0)
            else:
                raise ValueError("Invalid number of wcs dimensions: {0}".format(self.wcs.naxis))
            return (int(coord[0].item()), int(coord[1].item()))

        return tuple(int(coord) for coord in sky_coord)


class Observation():
    """Data and metadata for a single set of observations

    Attributes
    ----------
    images: array or tensor
        3D data cube (channels, Ny, Nx) of the image in each band.
    frame: a `scarlet.Frame` instance
        The spectral and spatial characteristics of these data
    weights: array or tensor
        Weight for each pixel in `images`.
        If a set of masks exists for the observations then
        then any masked pixels should have their `weight` set
        to zero.
    padding: int
        Number of pixels to pad each side with, in addition to
        half the width of the PSF, for FFTs. This is needed to
        prevent artifacts from the FFT.
    """

    def __init__(self, images, psfs=None, weights=None, wcs=None, channels=None, padding=10):
        """Create an Observation

        Parameters
        ---------
        images: array or tensor
            3D data cube (channels, Ny, Nx) of the image in each band.
        psfs: array or tensor
            PSF for each band in `images`.
        weights: array or tensor
            Weight for each pixel in `images`.
            If a set of masks exists for the observations then
            then any masked pixels should have their `weight` set
            to zero.
        wcs: TBD
            World Coordinate System associated with the images.
        channels: list of hashable elements
            Names/identifiers of spectral channels
        padding: int
            Number of pixels to pad each side with, in addition to
            half the width of the PSF, for FFTs. This is needed to
            prevent artifacts from the FFT.
        """
        self.frame = Frame(images.shape, wcs=wcs, psfs=psfs, channels=channels, dtype=images.dtype)

        self.images = np.array(images)
        if weights is not None:
            self.weights = np.array(weights)
        else:
            self.weights = 1

        self._padding = padding

    def match(self, model_frame):
        """Match the frame of `Blend` to the frame of this observation.

        The method sets up the mappings in spectral and spatial coordinates,
        which includes a spatial selection, computing PSF difference kernels
        and filter transformations.

        Parameters
        ---------
        model_frame: a `scarlet.Frame` instance
            The frame of `Blend` to match

        Returns
        -------
        None
        """

        if self.frame.dtype != model_frame.dtype:
            msg = "Dtypes of model and observation different. Casting observation to {}".format(model_frame.dtype)
            logger.warning(msg)
            self.frame.dtype = model_frame.dtype
            self.images = self.images.astype(model_frame.dtype)
            if type(self.weights) is np.ndarray:
                self.weights = self.weights.astype(model_frame.dtype)
            if self.frame._psfs is not None:
                self.frame._psfs = self.frame._psfs.astype(model_frame.dtype)

        #  channels of model that are represented in this observation
        self._band_slice = slice(None)
        if self.frame.channels is not model_frame.channels:
            assert self.frame.channels is not None and model_frame.channels is not None
            bmin = model_frame.channels.index(self.frame.channels[0])
            bmax = model_frame.channels.index(self.frame.channels[-1])
            self._band_slice = slice(bmin, bmax + 1)

        self._diff_kernels_fft = None
        if self.frame.psfs is not model_frame.psfs:
            assert self.frame.psfs is not None and model_frame.psfs is not None
            # First we setup the parameters for the model -> observation FFTs
            # Make the PSF stamp wider due to errors when matching PSFs
            psf_shape = np.array(self.frame.psfs.shape)
            psf_shape[1:] += self._padding
            conv_shape = np.array(model_frame.shape) + psf_shape - 1
            conv_shape[0] = model_frame.shape[0]

            # Choose the optimal shape for FFTPack DFT
            self._fftpack_shape = [fftpack.helper.next_fast_len(d) for d in conv_shape[1:]]

            # autograd.numpy.fft does not currently work
            # if the last dimension is odd
            while self._fftpack_shape[-1] % 2 != 0:
                _shape = self._fftpack_shape[-1] + 1
                self._fftpack_shape[-1] = fftpack.helper.next_fast_len(_shape)


            # Store the pre-fftpack optimization slices
            self.slices = tuple(([slice(s) for s in conv_shape]))

            # Now we setup the parameters for the psf -> kernel FFTs
            shape = np.array(self.frame.psfs.shape) + np.array(model_frame.psfs.shape) - 1
            shape[0] = np.array(self.frame.psfs.shape[0])

            #Fast fft shapes for kernels
            _fftpack_shape = [fftpack.helper.next_fast_len(d) for d in shape[1:]]

            while _fftpack_shape[-1] % 2 != 0:
                k_shape = np.array(_fftpack_shape) + 1
                _fftpack_shape = [fftpack.helper.next_fast_len(k_s) for k_s in k_shape]

            # fft of the target psf
            target_fft = np.fft.rfftn(model_frame.psfs, _fftpack_shape, axes=(1, 2))

            # fft of the observation's PSFs in each band
            _psf_fft = np.fft.rfftn(self.frame.psfs, _fftpack_shape, axes=(1, 2))

            # Diff kernel between observation and target psf in Fourrier
            kernels = np.fft.ifftshift(np.fft.irfftn(_psf_fft / target_fft, _fftpack_shape, axes=(1, 2)), axes=(1, 2))

            if kernels.shape[1] % 2 == 0 :
                kernels = kernels[:, 1:, 1:]

            kernels = _centered(kernels, psf_shape)

            self._diff_kernels_fft = np.fft.rfftn(kernels, self._fftpack_shape, axes=(1, 2))

        return self

    def _convolve(self, model):
        """Convolve the model in a single band
        """
        model_fft = np.fft.rfftn(model, self._fftpack_shape, axes=(1, 2))
        convolved = np.fft.irfftn(model_fft * self._diff_kernels_fft, self._fftpack_shape, axes=(1, 2))[self.slices]
        return _centered(convolved, model.shape)

    def render(self, model):
        """Convolve a model to the observation frame

        Parameters
        ----------
        model: array
            The model from `Blend`

        Returns
        -------
        model_: array
            The convolved `model` in the observation frame
        """
        model_ = model[self._band_slice, :, :]
        if self._diff_kernels_fft is not None:
            model_ = self._convolve(model_)

        return model_

    def get_loss(self, model):
        """Computes the loss/fidelity of a given model wrt to the observation

        Parameters
        ----------
        model: array
            The model from `Blend`

        Returns
        -------
        result: array
            Scalar tensor with the likelihood of the model
            given the image data
        """

        model = self.render(model)

        return 0.5 * np.sum((self.weights * (model - self.images)) ** 2)


class LowResObservation(Observation):

    def __init__(self, images, wcs=None, psfs=None, weights=None, channels=None, padding=3, operator = 'exact'):

        assert wcs is not None, "WCS is necessary for LowResObservation"
        assert psfs is not None, "PSFs are necessary for LowResObservation"
        assert operator in ['exact', 'bilinear', 'SVD']

        super().__init__(images, wcs=wcs, psfs=psfs, weights=weights, channels=channels, padding=padding)

    def make_operator(self, shape, psf):
        '''Builds the resampling and convolution operator
        Builds the matrix that expresses the linear operation of resampling a function evaluated on a grid with coordinates
        'coord_lr' to a grid with shape 'shape', and convolving by a kernel p
        Parameters
        ------
        shape: tuple
            shape of the high resolution scene
        coord_lr: array
            coordinates of the overlapping pixels from the low resolution grid in the high resolution grid frame.
        p: array
            convolution kernel (PSF)
        Returns
        -------
        mat: array
            the convolution-resampling matrix
        '''
        B, Ny, Nx = shape
        y_hr, x_hr = np.where(np.zeros((Ny, Nx)) == 0)
        y_lr, x_lr = self._coord_lr
        mat = np.zeros((Ny * Nx, x_lr.size))

        import scipy.signal as scp

        for m in range(np.size(x_lr)):
            line = scp.fftconvolve(self._ker[m], psf, mode='same').flatten()
            mat[:, m] = line/np.sum(line)
        return mat

    def match_psfs(self, psf_hr, wcs_hr):
        '''psf matching between different dataset
        Matches PSFS at different resolutions by interpolating psf_lr on the same grid as psf_hr
        Parameters
        ----------
        psf_hr: array
            centered psf of the high resolution scene
        psf_lr: array
            centered psf of the low resolution scene
        wcs_hr: WCS object
            wcs of the high resolution scene
        wcs_lr: WCS object
            wcs of the low resolution scene
        Returns
        -------
        psf_match_hr: array
            high rresolution psf at mactching size
        psf_match_lr: array
            low resolution psf at matching size and resolution
        '''

        psf_lr = self.frame.psfs
        wcs_lr = self.frame.wcs

        ny_hr, nx_hr = psf_hr.shape
        npsf, ny_lr, nx_lr = psf_lr.shape

        # Createsa wcs for psfs centered around the frame center
        psf_wcs_hr = wcs_hr.deepcopy()
        psf_wcs_lr = wcs_lr.deepcopy()

        if psf_wcs_hr.naxis == 2:
            psf_wcs_hr.wcs.crval = 0., 0.
            psf_wcs_hr.wcs.crpix = ny_hr / 2.+1, nx_hr / 2.+1
        elif psf_wcs_hr.naxis == 3:
            psf_wcs_hr.wcs.crval = 0., 0., 0.
            psf_wcs_hr.wcs.crpix = ny_hr / 2+1, nx_hr / 2., 0.
        if psf_wcs_lr.naxis == 2:
            psf_wcs_lr.wcs.crval = 0., 0.
            psf_wcs_lr.wcs.crpix = ny_lr / 2.+1, nx_lr / 2.+1
        elif psf_wcs_lr.naxis == 3:
            psf_wcs_lr.wcs.crval = 0., 0., 0.
            psf_wcs_lr.wcs.crpix = ny_lr / 2.+1, nx_lr / 2.+1, 0

        p_lr, p_hr, pover_hr = resampling.match_patches(psf_hr.shape, psf_lr.data.shape[1:], psf_wcs_hr, psf_wcs_lr, psf = True)

        npsf_y = np.max(pover_hr[0])-np.min(pover_hr[0])+1
        npsf_x = np.max(pover_hr[1])-np.min(pover_hr[1])+1

        psf_match_lr = interpolation.sinc_interp(pover_hr, p_hr[::-1],
                                                 psf_lr[:,p_lr[0], p_lr[1]]).reshape(npsf, npsf_y, npsf_x)
        psf_match_hr = psf_hr[pover_hr[0], pover_hr[1]].reshape(npsf_y, npsf_x)

        assert np.shape(psf_match_lr[0]) == np.shape(psf_match_hr)

        psf_match_hr /= np.sum(psf_match_hr)
        psf_match_lr /= np.sum(psf_match_lr)
        return psf_match_hr[np.newaxis, :], psf_match_lr

    def build_diffkernel(self, model_frame):
        '''Builds the differential convolution kernel between the observation and the frame psfs

        Parameters
        ----------
        model_frame: Frame object
            the frame of the model (hehehe)
        Returns
        -------
        diff_psf: array
            the differential psf between observation and frame psfs.
        '''
        # Compute diff kernel at hr
        whr = model_frame.wcs

        # Reference PSF
        _target = model_frame.psfs[0, :, :]
        _shape = model_frame.shape

        _fftpack_shape = [fftpack.helper.next_fast_len(d) for d in _target.shape]

        while _fftpack_shape[-1] % 2 != 0:
            k_shape = np.array(_fftpack_shape) + 1
            _fftpack_shape = [fftpack.helper.next_fast_len(k_s) for k_s in k_shape]

        # Computes spatially matching observation and target psfs. The observation psf is also resampled \\
        # to the model frame resolution
        new_target, observed_psf = self.match_psfs(_target, whr)
        target_fft = np.fft.rfftn(new_target[0], _fftpack_shape)
        sel = target_fft == 0
        observed_fft = np.fft.rfftn(observed_psf, _fftpack_shape, axes=(1, 2))

        # Computes the diff kernel in Fourier
        kernel_fft = observed_fft / target_fft
        kernel_fft[:, sel] = 0
        kernel = np.fft.irfftn(kernel_fft, _fftpack_shape, axes=(1, 2))
        kernel = np.fft.ifftshift(kernel, axes=(1, 2))

        if kernel.shape[1] % 2 == 0:
            kernel = kernel[:, 1:, 1:]

        kernel = _centered(kernel, observed_psf.shape)
        diff_psf = kernel / kernel.sum()

        return diff_psf

    def match(self, model_frame):

        if self.frame.dtype != model_frame.dtype:
            msg = "Dtypes of model and observation different. Casting observation to {}".format(model_frame.dtype)
            logger.warning(msg)
            self.frame.dtype = model_frame.dtype
            self.images = self.images.astype(model_frame.dtype)
            if type(self.weights) is np.ndarray:
                self.weights = self.weights.astype(model_frame.dtype)
            if self.frame._psfs is not None:
                self.frame._psfs = self.frame._psfs.astype(model_frame.dtype)

        #  channels of model that are represented in this observation
        self._band_slice = slice(None)
        if self.frame.channels is not model_frame.channels:
            bmin = model_frame.channels.index(self.frame.channels[0])
            bmax = model_frame.channels.index(self.frame.channels[-1])
            self._band_slice = slice(bmin, bmax+1)

        # Angle between datasets
        rot = np.cross(np.sum(self.frame.wcs.wcs.pc, axis=0)[:2], np.sum(model_frame.wcs.wcs.pc, axis=0)[:2])
        isrot = (np.abs(rot) % np.pi) < np.finfo(float).eps

        # Get pixel coordinates in each frame.
        coord_lr, coord_hr, coordhr_over = resampling.match_patches(model_frame.shape, self.frame.shape,
                                                                    model_frame.wcs, self.frame.wcs, isrot = isrot)
        #Coordinates of overlapping low resolutions pixels at low resolution
        self._coord_lr = coord_lr
        #Coordinates of overlaping low resolution pixels in high resolution frame
        self._coord_hr = coord_hr
        #Coordinates for all model frame pixels
        frame_coord = (np.array(range(model_frame.Ny)), np.array(range(model_frame.Nx)))

        diff_psf = self.build_diffkernel(model_frame)

        #Padding the psf to the frame size
        #At the moment, this only handles the case where size(psf)<size(frame), which,
        # I think is the only one that should arise anyway
        padx = np.int((model_frame.shape[1] - diff_psf.shape[1]) / 2)
        pady = np.int((model_frame.shape[2] - diff_psf.shape[2]) / 2)
        diff_psf = np.pad(diff_psf, ((0, 0), (padx, padx), (pady, pady)), 'constant')

        nky, nkx = diff_psf[0].shape
        # Coordinates for all psf pixels in model frame (centered on the frame's centre)
        psf_coord = frame_coord#(np.array(range(nky))-nky/2+model_frame.Ny/2, np.array(range(nkx))-nkx/2+model_frame.Nx/2)

        #1D convolutions convolutions of the model are done along the smaller axis
        if self.frame.Ny <= self.frame.Nx:
            # Interpolation kernel for resampling
            _ker_psf = resampling.make_ker1D(frame_coord, self._coord_hr, axis = 1)
            self._ker_model = resampling.make_ker1D(psf_coord, self._coord_hr, axis = 0)
        else:
            # Interpolation kernel for resampling
            _ker_psf = resampling.make_ker1D(frame_coord,self._coord_hr, axis = 0)
            self._ker_model = resampling.make_ker1D(psf_coord, self._coord_hr, axis = 1)

        self.ker_model_fft = None

        # Computes the resampling/convolution matrix
        resconv_op = []
        for dpsf in diff_psf:
            if self.frame.Ny <= self.frame.Nx:
                resconv_temp = convolve_1Dfft(dpsf, _ker_psf, axis = 1)
                resconv_shape = np.shape(resconv_temp)

                resconv_op.append(np.reshape(resconv_temp,(resconv_shape[0]*resconv_shape[1], resconv_shape[2])))
            else:
                resconv_temp = convolve_1Dfft(dpsf, _ker_psf, axis=0)
                resconv_shape = np.shape(resconv_temp)

                resconv_op.append(np.reshape(resconv_temp, (resconv_shape[0], resconv_shape[1] * resconv_shape[2])))
        self._resconv_op = np.array(resconv_op, dtype=self.frame.dtype)

        return self


    def _render(self, model):
        """Resample and convolve a model in the observation frame
        Parameters
        ----------
        model: array
            The model in some other data frame.
        Returns
        -------
        model_: array
            The convolved and resampled `model` in the observation frame.
        """

        model_ = model[self._band_slice,:,:]
        model_image = []
        for c in range(self.frame.C):

            if self.frame.Ny <= self.frame.Nx:
                if self.ker_model_fft is None:

                    model_conv1d, _ker_model_fft = convolve_1Dfft(model_[c], self._ker_model, axis = 0, return_fft = True)
                    self.ker_model_fft = _ker_model_fft

                else:
                    model_conv1d = convolve_1Dfft(model_[c], self._ker_model,
                                                             ker_fft = self.ker_model_fft, axis = 0, return_fft = False)
            else:
                if self.ker_model_fft is None:

                    model_conv1d, _ker_model_fft = convolve_1Dfft(model_[c], self._ker_model, axis = 1, return_fft = True)
                    self.ker_model_fft = _ker_model_fft
                else:
                    model_conv1d = convolve_1Dfft(model_[c], self._ker_model,
                                                  ker_fft=self.ker_model_fft, axis = 1, return_fft=False)

            model_shape = np.shape(model_conv1d)

            if self.frame.Ny <= self.frame.Nx:
                #I DON'T KNOW why this axis is inverted and IT KILLS ME!!
                model_image.append(np.dot(model_conv1d.reshape(model_shape[0],model_shape[1] * model_shape[2]), self._resconv_op[c])[::-1, :])
            else:
                model_image.append(np.dot(self._resconv_op[c],
                                          model_conv1d.reshape(model_shape[0]*model_shape[1], model_shape[2])))
        model_image = np.array(model_image, dtype=self.frame.dtype)

        return model_image

    def render(self, model):
        """Resample and convolve a model in the observation frame for display only!
        Parameters
        ----------
        model: array
            The model in some other data frame.
        Returns
        -------
        model_: array
            The convolved and resampled `model` in the observation frame.
        """
        img = np.zeros(self.frame.shape)

        img[:, np.min(self._coord_lr[0]).astype(int):np.max(self._coord_lr[0]).astype(int)+1,
                         np.min(self._coord_lr[1]).astype(int):np.max(self._coord_lr[1]).astype(int)+1] = \
            self._render(model)
        return img

    def get_loss(self, model):
        """Computes the loss/fidelity of a given model wrt to the observation
        Parameters
        ----------
        model: array
            A model from `Blend`
        Returns
        -------
        loss: float
            Loss of the model
        """

        model_ = self._render(model)

        return 0.5 * np.sum((self.weights * (
                model_ - self.images[:, np.min(self._coord_lr[0]).astype(int):np.max(self._coord_lr[0]).astype(int)+1,
                         np.min(self._coord_lr[1]).astype(int):np.max(self._coord_lr[1]).astype(int)+1])) ** 2)
