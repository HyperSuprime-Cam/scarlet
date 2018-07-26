from __future__ import print_function, division
import warnings

import numpy as np
import scipy.sparse
import proxmin.utils
from .cache import Cache

class LinearFilter:
    """A filter that can be applied to an image

    This acts like a sparse diagonal matrix that applies an
    image of weights to a 2D matrix.
    """
    def __init__(self, values, coords=None, center=None):
        """Initialize the Filter

        Parameters
        ----------
        values: 2D or 1D array-like
            Weights to apply to the filter.
            If `coords` is `None` than either `values` must
            be 2D with an odd number of rows and columns
            (and the current pixel in the center) or the
            location of the current pixel (`center`) in
            `values` must be specified.
        coords: 2D or 1D array-like
            Relative coordinates from the current pixel
            for each weight in `img`
            (so [0,0] is the current pixel).
        center: array-like
            index of the current pixel in `values`.
            If a set of `coords` is not defined, they can be
            created as long as either the `center` location in
            `values` is specified or `values` has an odd number
            of rows and columns.
            For example, if `values` is `[[0,1],[2,3]]` and the
            current pixel is the upper right element, then
            `center=[0,0]` (`values=0`). If current pixel is the top right then
            `center=[0,1]` (`values=1`).
        """
        if coords is None:
            # Attempt to automatically create coordinate grid
            if len(values.shape)!=2:
                raise ValueError("Either `values` must be 2D or `coords` must be specified")
            if center is None:
                if values.shape[0] % 2 == 0 or values.shape[1] % 2 == 0:
                    msg = """Ambiguous center of the `values` array,
                             you must either specify a set of `coords` or use
                             a `values` array with an odd number of rows and columns"""
                    raise ValueError(msg)
                center = [values.shape[0]//2, values.shape[1]//2]
            x = np.arange(values.shape[1])
            y = np.arange(values.shape[0])
            x,y = np.meshgrid(x,y)
            x -= center[1]
            y -= center[0]
            coords = np.dstack([y,x])
        else:
            coords = np.array(coords)
        values = np.array(values)
        self._flat_values = np.array(values).reshape(-1)
        self._flat_coords = coords.reshape(-1,2)
        assert(np.all(values.shape==coords.shape[:-1]))
        assert(coords.shape[-1] == 2)
        # remove elements with zero value
        non_zero = self._flat_values != 0
        self._flat_values = self._flat_values[non_zero]
        self._flat_coords = self._flat_coords[non_zero]
        self._slices = get_filter_slices(self._flat_coords)

    @property
    def T(self):
        """Transpose the filter
        """
        return LinearFilter(self._flat_values, -self._flat_coords)

    def dot(self, X):
        """Apply the filter to an image or combine filters

        Parameters
        ----------
        X: 2D numpy array or `LinearFilter` or `LinearFilterChain`
            Array to apply the filter to, or chain of filters to
            prepend this filter to.

        Returns
        -------
        result: 2D numpy array or `LinearFilterChain`
            If `X` is an array, this is the result of the
            filter applied to `X`.
            If `X` is not an image but is another filter
            (or chain of filters) then a new `LinearFilterChain` is
            returned with this one prepended.
        """
        from .operators_pybind11 import apply_filter

        if isinstance(X, LinearFilter):
            return LinearFilterChain([self, X])
        elif isinstance(X, LinearFilterChain):
            X.filters.insert(0,self)
            return X
        else:
            result = np.empty(X.shape, dtype=X.dtype)
            apply_filter(X, self._flat_values, self._slices[0], self._slices[1],
                                 self._slices[2], self._slices[3], result)
            return result

    def get_filter_slices(self, coords):
        """Get the slices in x and y to apply a filter
        """
        z = np.zeros((len(coords),), dtype=int)
        # Set the y slices
        y_start = np.max([z, coords[:,0]], axis=0)
        y_end = -np.min([z, coords[:,0]], axis=0)
        # Set the x slices
        x_start = np.max([z, coords[:,1]], axis=0)
        x_end = -np.min([z, coords[:,1]], axis=0)
        return y_start, y_end, x_start, x_end


class LinearFilterChain:
    """Chain of `LinearFilter` objects

    Because `LinearFilter` objects are not really arrays,
    this class keeps track of the order of a series of filters.
    """
    def __init__(self, filters):
        """Initialize the class

        Parameters
        ----------
        filters: list
            List of `LinearFilter` objects, in the order from
            left to right. So the last element in `filters` is
            applied to an image first.
        """
        self.filters = filters

    @property
    def T(self):
        """Transpose the list of filters

        Reverse the order and transpose each `LinearFilter` in
        `self.filters`.
        """
        return LinearFilterChain([f.T for f in self.filters[::-1]])

    def dot(self, X):
        """Apply the filters

        Apply the filters in reverse order,
        starting with the last element, to the
        image `X`.

        Parameters
        ----------
        X: 2D numpy array
            Image to apply the filters to.

        Returns
        -------
        result: 2D numpy array or `LinearFilterChain`
            If `X` is an array, this will be the result of
            applying all of the filters in `X`.
            Otherwise this returns a new `LinearFilterChain`
            that appends `X`.
        """
        if isinstance(X, LinearFilter):
            self.filters.append(X)
        elif isinstance(X, LinearFilterChain):
            for f in X.filters:
                self.filters.append(f)
        else:
            _filters = self.filters[::-1]
            result = X
            for f in _filters:
                result = f.dot(result)
            return result
        return self

class Translation(LinearFilter):
    """Translation in x and y

    Uses the Lanczos3 kernel to translate 2D component morpologies to a
    given subpixel position on the image.
    """
    def __init__(self, dy=0, dx=0):
        """Initialize the filter

        Parameters
        ----------
        dy: float
            Fractional amount (from 0 to 1) to
            shift the image in the y-direction
        dx: float
            Fractional amount (from 0 to 1) to
            shift the image in the x-direction
        """
        self.dy = dy
        self.dx = dx
        a = 3
        ij = np.arange(-a + 1, a)

        # cache coords and slices because they are the same every time
        key = None
        coord_name = "Translation.coord"
        slice_name = "Translation.slice"
        try:
            self._flat_coords = Cache.check(coord_name, key)
            self._slices = Cache.check(slice_name, key)
        except KeyError:
            self._flat_coords = np.dstack(np.meshgrid(ij,ij, indexing='ij')).reshape(-1, 2)
            self._slices = self.get_filter_slices(self._flat_coords)
            Cache.set(coord_name, key, self._flat_coords)
            Cache.set(slice_name, key, self._slices)

        # no point in caching the Lanczos kernel for every dy/dx
        Ly = np.sinc(dy - ij)*np.sinc((dy - ij)/a)
        Lx = np.sinc(dx - ij)*np.sinc((dx - ij)/a)
        self._flat_values = np.outer(Ly, Lx).flatten()

    @property
    def T(self):
        """Transpose the filter
        """
        return Translation(-self.dy, -self.dx)

class Gamma:
    """Combination of Linear (x,y) Transformation and PSF Convolution

    Since the translation operators and PSF convolution operators both act
    on the de-convolved, centered S matrix, we can instead think of the translation
    operators translating the PSF convolution kernel, making a single transformation
    Gamma = Ty.P.Tx, where Tx,Ty are the translation operators and P is the PSF
    convolution operator.
    """
    def __init__(self, psfs=None, center=None, dy=0, dx=0):
        """Constructor

        Parameters
        ----------
        psfs: array-like, default=`None`
            An array/list of images with a PSF image for each band.
            If `psfs` is `None` then no PSF convolution is performed, but
            a number of bands `B` must be specified.
        center: integer array-like, default=`None`
            Center of the PSF. If `center` is `None` and a set of `psfs` is given,
            the central pixel of `psfs[0]` is used.
        dy: float
            Fractional shift in the y direction
        dx: float
            Fractional shift in the x direction
        """
        self.psfs = psfs

        # Create the PSF filter for each band
        if psfs is not None:
            self._update_psf(psfs, center)
            self.B = len(psfs)
        else:
            self.psfFilters = None
            self.B = None
        # Create the transformation matrices
        self._update_translation(dy, dx)

    def _update_psf(self, psfs, center=None):
        """Update the psf convolution filter
        """
        self.psfFilters = []
        for psf in psfs:
            self.psfFilters.append(LinearFilter(psf, center=center))

    def _update_translation(self, dy=0, dx=0):
        """Update the translation filter
        """
        self.dx = dx
        self.dy = dy
        self.translation = Translation(dy, dx)

    def update(self, psfs=None, center=None, dx=None, dy=None):
        """Update the psf convolution filter and/or the translations

        See `self.__init__` for parameter descriptions
        """
        if psfs is not None:
            self._update_psf(psfs, center)
        if dx is not None or dy is not None:
            if dx is None:
                dx = self.dx
            if dy is None:
                dy = self.dy
            self._update_translation(dy, dx)

    def __call__(self, dyx=None):
        """Build a Gamma "Matrix"

        Combine the translation and PSF convolution into
        a single class that acts like a linear operator.

        Parameters
        ----------
        dyx: 2D array-like, default=`None`
            Fractional shift in position in `[dy,dx]`.
            If `dyx` is `None`, then the already built
            translation matrix is used.
        """
        translation = Translation(*dyx)
        if self.psfFilters is None:
            gamma = translation
        else:
            gamma = []
            for b in range(self.B):
                gamma.append(LinearFilterChain([translation, self.psfFilters[b]]))
        return gamma

def getPSFOp(psf, imgShape):
    """Create an operator to convolve intensities with the PSF

    Given a psf image ``psf`` and the shape of the blended image ``imgShape``,
    make a banded matrix out of all non-zero pixels in ``psfImg`` that acts as
    the PSF operator.
    """

    warnings.warn("The 'psfOp' is deprecated, use 'LinearFilter' instead")
    name = "getPSFOp"
    key = tuple(imgShape)
    try:
        psfOp = Cache.check(name, key)
    except KeyError:
        height, width = imgShape
        size = width * height

        # Calculate the coordinates of the pixels in the psf image above the threshold
        indices = np.where(psf != 0)
        indices = np.dstack(indices)[0]
        # assume all PSF images have odd dimensions and are centered!
        cy, cx = psf.shape[0]//2, psf.shape[1]//2
        coords = indices-np.array([cy,cx])

        # Create the PSF Operator
        offsets, slices, slicesInv = getOffsets(width, coords)
        psfDiags = [psf[y,x] for y,x in indices]
        psfOp = scipy.sparse.diags(psfDiags, offsets, shape=(size, size), dtype=np.float64)
        psfOp = psfOp.tolil()

        # Remove entries for pixels on the left or right edges
        cxRange = np.unique([cx for cy,cx in coords])
        for h in range(height):
            for y,x in coords:
                # Left edge
                if x<0 and width*(h+y)+x>=0 and h+y<=height:
                    psfOp[width*h, width*(h+y)+x] = 0

                    # Pixels closer to the left edge
                    # than the radius of the psf
                    for x_ in cxRange[cxRange<0]:
                        if (x<x_ and
                            width*h-x_>=0 and
                            width*(h+y)+x-x_>=0 and
                            h+y<=height
                        ):
                            psfOp[width*h-x_, width*(h+y)+x-x_] = 0

                # Right edge
                if x>0 and width*(h+1)-1>=0 and width*(h+y+1)+x-1>=0 and h+y<=height and width*(h+1+y)+x-1<size:
                    psfOp[width*(h+1)-1, width*(h+y+1)+x-1] = 0

                    for x_ in cxRange[cxRange>0]:
                        # Near right edge
                        if (x>x_ and
                            width*(h+1)-x_-1>=0 and
                            width*(h+y+1)+x-x_-1>=0 and
                            h+y<=height and
                            width*(h+1+y)+x-x_-1<size
                        ):
                            psfOp[width*(h+1)-x_-1, width*(h+y+1)+x-x_-1] = 0

        # Return the transpose, which correctly convolves the data with the PSF
        psfOp = proxmin.utils.MatrixAdapter(psfOp.T.tocoo(), axis=1)
        Cache.set(name, key, psfOp)

    return psfOp

def getZeroOp(shape):
    size = shape[0]*shape[1]
    name = "getZeroOp"
    key = tuple(shape)
    try:
        L = Cache.check(name, key)
    except KeyError:
        # matrix with ones on diagonal shifted by k, here out of matrix: all zeros
        L = proxmin.utils.MatrixAdapter(scipy.sparse.eye(size, k=size), axis=1)
        L._spec_norm = 0
        Cache.set(name, key, L)
    return L

def getIdentityOp(shape):
    size = shape[0]*shape[1]
    name = "getIdentityOp"
    key = tuple(shape)
    try:
        L = Cache.check(name, key)
    except KeyError:
        # matrix with ones on diagonal shifted by k, here out of matrix: all zeros
        L = proxmin.utils.MatrixAdapter(scipy.sparse.identity(size), axis=1)
        L._spec_norm = 1
        Cache.set(name, key, L)
    return L

def getSymmetryOp(shape):
    """Create a linear operator to symmetrize an image

    Given the ``shape`` of an image, create a linear operator that
    acts on the flattened image to return its symmetric version.
    """
    size = shape[0]*shape[1]
    name = "getSymmetryOp"
    key = tuple(shape)
    try:
        symmetryOp = Cache.check(name, key)
    except KeyError:
        idx = np.arange(shape[0]*shape[1])
        sidx = idx[::-1]
        symmetryOp = getIdentityOp(shape).L
        symmetryOp -= scipy.sparse.coo_matrix((np.ones(size),(idx, sidx)), shape=(size,size))
        symmetryOp = proxmin.utils.MatrixAdapter(symmetryOp, axis=1)
        _ = symmetryOp.spectral_norm
        Cache.set(name, key, symmetryOp)
    return symmetryOp

def getOffsets(width, coords=None):
    """Get the offset and slices for a sparse band diagonal array

    For an operator that interacts with its neighbors we want a band diagonal matrix,
    where each row describes the 8 pixels that are neighbors for the reference pixel
    (the diagonal). Regardless of the operator, these 8 bands are always the same,
    so we make a utility function that returns the offsets (passed to scipy.sparse.diags).

    See `diagonalizeArray` for more on the slices and format of the array used to create
    NxN operators that act on a data vector.
    """
    # Use the neighboring pixels by default
    if coords is None:
        coords = [(-1,-1), (-1,0), (-1, 1), (0,-1), (0,1), (1, -1), (1,0), (1,1)]
    offsets = [width*y+x for y,x in coords]
    slices = [slice(None, s) if s<0 else slice(s, None) for s in offsets]
    slicesInv = [slice(-s, None) if s<0 else slice(None, -s) for s in offsets]
    return offsets, slices, slicesInv

def diagonalizeArray(arr, shape=None, dtype=np.float64):
    """Convert an array to a matrix that compares each pixel to its neighbors

    Given an array with length N, create an 8xN array, where each row will be a
    diagonal in a diagonalized array. Each column in this matrix is a row in the larger
    NxN matrix used for an operator, except that this 2D array only contains the values
    used to create the bands in the band diagonal matrix.

    Because the off-diagonal bands have less than N elements, ``getOffsets`` is used to
    create a mask that will set the elements of the array that are outside of the matrix to zero.

    ``arr`` is the vector to diagonalize, for example the distance from each pixel to the peak,
    or the angle of the vector to the peak.

    ``shape`` is the shape of the original image.
    """
    if shape is None:
        height, width = arr.shape
        data = arr.flatten()
    elif len(arr.shape)==1:
        height, width = shape
        data = np.copy(arr)
    else:
        raise ValueError("Expected either a 2D array or a 1D array and a shape")
    size = width * height

    # We hard code 8 rows, since each row corresponds to a neighbor
    # of each pixel.
    diagonals = np.zeros((8, size), dtype=dtype)
    mask = np.ones((8, size), dtype=bool)
    offsets, slices, slicesInv = getOffsets(width)
    for n, s in enumerate(slices):
        diagonals[n][slicesInv[n]] = data[s]
        mask[n][slicesInv[n]] = 0

    # Create a mask to hide false neighbors for pixels on the edge
    # (for example, a pixel on the left edge should not be connected to the
    # pixel to its immediate left in the flattened vector, since that pixel
    # is actual the far right pixel on the row above it).
    mask[0][np.arange(1,height)*width] = 1
    mask[2][np.arange(height)*width-1] = 1
    mask[3][np.arange(1,height)*width] = 1
    mask[4][np.arange(1,height)*width-1] = 1
    mask[5][np.arange(height)*width] = 1
    mask[7][np.arange(1,height-1)*width-1] = 1

    return diagonals, mask

def diagonalsToSparse(diagonals, shape, dtype=np.float64):
    """Convert a diagonalized array into a sparse diagonal matrix

    ``diagonalizeArray`` creates an 8xN array representing the bands that describe the
    interactions of a pixel with its neighbors. This function takes that 8xN array and converts
    it into a sparse diagonal matrix.

    See `diagonalizeArray` for the details of the 8xN array.
    """
    height, width = shape
    offsets, slices, slicesInv = getOffsets(width)
    diags = [diag[slicesInv[n]] for n, diag in enumerate(diagonals)]
    diagonalArr = scipy.sparse.diags(diags, offsets, dtype=dtype)
    return diagonalArr

def getRadialMonotonicWeights(shape, useNearest=True, minGradient=1):
    """Create the weights used for the Radial Monotonicity Operator

    This version of the radial monotonicity operator selects all of the pixels closer to the peak
    for each pixel and weights their flux based on their alignment with a vector from the pixel
    to the peak. In order to quickly create this using sparse matrices, its construction is a bit opaque.
    """

    name = "RadialMonotonicWeights"
    key = tuple(shape) + (useNearest, minGradient)
    try:
        cosNorm = Cache.check(name, key)
    except KeyError:

        # Center on the center pixel
        px = int(shape[1]/2)
        py = int(shape[0]/2)
        # Calculate the distance between each pixel and the peak
        size = shape[0]*shape[1]
        x = np.arange(shape[1])
        y = np.arange(shape[0])
        X,Y = np.meshgrid(x,y)
        X = X - px
        Y = Y - py
        distance = np.sqrt(X**2+Y**2)

        # Find each pixels neighbors further from the peak and mark them as invalid
        # (to be removed later)
        distArr, mask = diagonalizeArray(distance, dtype=np.float64)
        relativeDist = (distance.flatten()[:,None]-distArr.T).T
        invalidPix = relativeDist<=0

        # Calculate the angle between each pixel and the x axis, relative to the peak position
        # (also avoid dividing by zero and set the tan(infinity) pixel values to pi/2 manually)
        inf = X==0
        tX = X.copy()
        tX[inf] = 1
        angles = np.arctan2(-Y,-tX)
        angles[inf&(Y!=0)] = 0.5*np.pi*np.sign(angles[inf&(Y!=0)])

        # Calcualte the angle between each pixel and it's neighbors
        xArr, m = diagonalizeArray(X)
        yArr, m = diagonalizeArray(Y)
        dx = (xArr.T-X.flatten()[:, None]).T
        dy = (yArr.T-Y.flatten()[:, None]).T
        # Avoid dividing by zero and set the tan(infinity) pixel values to pi/2 manually
        inf = dx==0
        dx[inf] = 1
        relativeAngles = np.arctan2(dy,dx)
        relativeAngles[inf&(dy!=0)] = 0.5*np.pi*np.sign(relativeAngles[inf&(dy!=0)])

        # Find the difference between each pixels angle with the peak
        # and the relative angles to its neighbors, and take the
        # cos to find its neighbors weight
        dAngles = (angles.flatten()[:, None]-relativeAngles.T).T
        cosWeight = np.cos(dAngles)
        # Mask edge pixels, array elements outside the operator (for offdiagonal bands with < N elements),
        # and neighbors further from the peak than the reference pixel
        cosWeight[invalidPix] = 0
        cosWeight[mask] = 0

        if useNearest:
            # Only use a single pixel most in line with peak
            cosNorm = np.zeros_like(cosWeight)
            columnIndices =  np.arange(cosWeight.shape[1])
            maxIndices = np.argmax(cosWeight, axis=0)
            indices = maxIndices*cosNorm.shape[1]+columnIndices
            indices = np.unravel_index(indices, cosNorm.shape)
            cosNorm[indices] = minGradient
            # Remove the reference for the peak pixel
            cosNorm[:,px+py*shape[1]] = 0
        else:
            # Normalize the cos weights for each pixel
            normalize = np.sum(cosWeight, axis=0)
            normalize[normalize==0] = 1
            cosNorm = (cosWeight.T/normalize[:,None]).T
            cosNorm[mask] = 0

        Cache.set(name, key, cosNorm)

    return cosNorm

def getRadialMonotonicOp(shape, useNearest=True, minGradient=1, subtract=True):
    """Create an operator to constrain radial monotonicity

    This version of the radial monotonicity operator selects all of the pixels closer to the peak
    for each pixel and weights their flux based on their alignment with a vector from the pixel
    to the peak. In order to quickly create this using sparse matrices, its construction is a bit opaque.
    """

    name = "RadialMonotonic"
    key = tuple(shape) + (useNearest, minGradient, subtract)
    try:
        monotonic = Cache.check(name, key)
    except KeyError:

        cosNorm = getRadialMonotonicWeights(shape, useNearest=useNearest, minGradient=1)
        cosArr = diagonalsToSparse(cosNorm, shape)

        # The identity with the peak pixel removed represents the reference pixels
        # Center on the center pixel
        px = int(shape[1]/2)
        py = int(shape[0]/2)
        # Calculate the distance between each pixel and the peak
        size = shape[0]*shape[1]
        diagonal = np.ones(size)
        diagonal[px+py*shape[1]] = -1
        if subtract:
            monotonic = cosArr-scipy.sparse.diags(diagonal, offsets=0)
        else:
            monotonic = cosArr
        monotonic = proxmin.utils.MatrixAdapter(monotonic.tocoo(), axis=1)
        _ = monotonic.spectral_norm
        Cache.set(name, key, monotonic)

    return monotonic


# ring-shaped masks around the peak
def getRingMask(im_shape, peak, outer, inner=0, flatten=False):
    height,width = im_shape
    x,y = np.meshgrid(np.arange(width), np.arange(height))
    r = np.sqrt((x-peak[1])**2 + (y-peak[0])**2)
    mask = (r < inner) | (r >= outer)
    if flatten:
        return mask.flatten()
    return mask

# odd-integer downsampling
def downsample(S, oversampling, mask=None):
    assert isinstance(oversampling, (int, long))
    if oversampling <= 1:
        return S
    else:
        height,width = S.shape
        height /= oversampling
        width /= oversampling
        Sd = np.zeros((height, width), dtype=S.dtype)
        if mask is None:
            S_ = S
        else:
            S_ = S*(~mask)
        # TODO: can we avoid the double loop?
        for h in range(height):
            for w in range(width):
                Sd[h,w] = S_[h*oversampling:(h+1)*oversampling,
                             w*oversampling:(w+1)*oversampling].sum() / oversampling**2
        return Sd

# construct spin-wave decomposition operator for given list of spin numbers m
# radial behavior can be specified as appropriate
def getSpinOp(ms, shape, thickness=4, peak=None, oversampling=21, radial_fct=lambda r:1./np.maximum(1,r)):
    """ Spin decomposition operator.

    The operator maps onto a basis function of R(r) exp(i m phi), where phi is
    the polar angle wrt to the peak (or, if None, the center of the image).

    The decomposition is performed in a set of concentric rings of fixed thickness.

    ms is a list of integers that indicate the requested spin numbers.
    thickness is the radial separation between the inner and outer ring radius.
    peak is (an optional) offset of the object from the image center.
    oversampling determine the higher-resolution grid for the in-pixel
    integration; it must be odd.
    radial_fct is the radial part of the spin basis function.

    """
    assert oversampling % 2 == 1
    assert hasattr(ms, '__iter__')

    height,width = shape
    assert height % 2 == 0 and width % 2 == 0

    if peak is None:
        peak = [height/2, width/2]
    x,y = np.meshgrid(np.arange(width*oversampling), np.arange(height*oversampling))
    x = x * 1./oversampling - peak[1]
    y = y * 1./oversampling - peak[0]
    # proper treatment of over & downsampling: center pixel location
    if oversampling > 1:
        x -= 0.5 - 0.5/oversampling
        y -= 0.5 - 0.5/oversampling
    # convert to polar
    r = np.sqrt(x**2 + y**2)
    phi = np.arctan2(y,x)

    # define series of radial ring-shaped masks (oversampled as well)
    r_limit = (np.min([peak[0], height-peak[0], peak[1], width-peak[1]]) - 1)*oversampling
    base = thickness*oversampling
    lims = [(base*(i+1), base*i) for i in range(r_limit/base)]
    mask_peak = ((peak[0]+0.5)*oversampling - 0.5, (peak[1]+0.5)*oversampling - 0.5)
    masks = [getRingMask(r.shape, mask_peak, outer, inner) for outer, inner in lims]

    Ss = []
    for i in range(len(ms)):
        m = ms[i]
        spin = radial_fct(r) * np.exp(1j*m*phi)
        for j in range(len(masks)):
            mask = masks[j]
            S = downsample(spin, oversampling, mask=mask).flatten()
            Ss.append(S)

    # TODO: make Ss sparse and split real and imaginary part
    return np.array(Ss)
