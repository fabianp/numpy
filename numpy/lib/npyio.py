__all__ = ['savetxt', 'loadtxt', 'genfromtxt', 'ndfromtxt', 'mafromtxt',
           'recfromtxt', 'recfromcsv', 'load', 'loads', 'save', 'savez',
           'savez_compressed', 'packbits', 'unpackbits', 'fromregex', 'DataSource']

import numpy as np
import format
import sys
import os
import sys
import itertools
import warnings
from operator import itemgetter

from cPickle import load as _cload, loads
from _datasource import DataSource
from _compiled_base import packbits, unpackbits

from _iotools import LineSplitter, NameValidator, StringConverter, \
                     ConverterError, ConverterLockError, ConversionWarning, \
                     _is_string_like, has_nested_fields, flatten_dtype, \
                     easy_dtype, _bytes_to_name

from numpy.compat import asbytes, asstr, asbytes_nested, bytes

if sys.version_info[0] >= 3:
    from io import BytesIO
else:
    from cStringIO import StringIO as BytesIO

_string_like = _is_string_like

def seek_gzip_factory(f):
    """Use this factory to produce the class so that we can do a lazy
    import on gzip.

    """
    import gzip

    class GzipFile(gzip.GzipFile):

        def seek(self, offset, whence=0):
            # figure out new position (we can only seek forwards)
            if whence == 1:
                offset = self.offset + offset

            if whence not in [0, 1]:
                raise IOError, "Illegal argument"

            if offset < self.offset:
                # for negative seek, rewind and do positive seek
                self.rewind()
                count = offset - self.offset
                for i in range(count // 1024):
                    self.read(1024)
                self.read(count % 1024)

        def tell(self):
            return self.offset


    if isinstance(f, str):
        f = GzipFile(f)
    elif isinstance(f, gzip.GzipFile):
        # cast to our GzipFile if its already a gzip.GzipFile

        try:
            name = f.name
        except AttributeError:
            # Backward compatibility for <= 2.5
            name = f.filename
        mode = f.mode

        f = GzipFile(fileobj=f.fileobj, filename=name)
        f.mode = mode

    return f



class BagObj(object):
    """
    BagObj(obj)

    Convert attribute look-ups to getitems on the object passed in.

    Parameters
    ----------
    obj : class instance
        Object on which attribute look-up is performed.

    Examples
    --------
    >>> from numpy.lib.npyio import BagObj as BO
    >>> class BagDemo(object):
    ...     def __getitem__(self, key): # An instance of BagObj(BagDemo)
    ...                                 # will call this method when any
    ...                                 # attribute look-up is required
    ...         result = "Doesn't matter what you want, "
    ...         return result + "you're gonna get this"
    ...
    >>> demo_obj = BagDemo()
    >>> bagobj = BO(demo_obj)
    >>> bagobj.hello_there
    "Doesn't matter what you want, you're gonna get this"
    >>> bagobj.I_can_be_anything
    "Doesn't matter what you want, you're gonna get this"

    """
    def __init__(self, obj):
        self._obj = obj
    def __getattribute__(self, key):
        try:
            return object.__getattribute__(self, '_obj')[key]
        except KeyError:
            raise AttributeError, key

def zipfile_factory(*args, **kwargs):
    import zipfile
    if sys.version_info >= (2, 5):
        kwargs['allowZip64'] = True
    return zipfile.ZipFile(*args, **kwargs)

class NpzFile(object):
    """
    NpzFile(fid)

    A dictionary-like object with lazy-loading of files in the zipped
    archive provided on construction.

    `NpzFile` is used to load files in the NumPy ``.npz`` data archive
    format. It assumes that files in the archive have a ".npy" extension,
    other files are ignored.

    The arrays and file strings are lazily loaded on either
    getitem access using ``obj['key']`` or attribute lookup using
    ``obj.f.key``. A list of all files (without ".npy" extensions) can
    be obtained with ``obj.files`` and the ZipFile object itself using
    ``obj.zip``.

    Attributes
    ----------
    files : list of str
        List of all files in the archive with a ".npy" extension.
    zip : ZipFile instance
        The ZipFile object initialized with the zipped archive.
    f : BagObj instance
        An object on which attribute can be performed as an alternative
        to getitem access on the `NpzFile` instance itself.

    Parameters
    ----------
    fid : file or str
        The zipped archive to open. This is either a file-like object
        or a string containing the path to the archive.
    own_fid : bool, optional
        Whether NpzFile should close the file handle.
        Requires that `fid` is a file-like object.

    Examples
    --------
    >>> from tempfile import TemporaryFile
    >>> outfile = TemporaryFile()
    >>> x = np.arange(10)
    >>> y = np.sin(x)
    >>> np.savez(outfile, x=x, y=y)
    >>> outfile.seek(0)

    >>> npz = np.load(outfile)
    >>> isinstance(npz, np.lib.io.NpzFile)
    True
    >>> npz.files
    ['y', 'x']
    >>> npz['x']  # getitem access
    array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    >>> npz.f.x  # attribute lookup
    array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])

    """
    def __init__(self, fid, own_fid=False):
        # Import is postponed to here since zipfile depends on gzip, an optional
        # component of the so-called standard library.
        _zip = zipfile_factory(fid)
        self._files = _zip.namelist()
        self.files = []
        for x in self._files:
            if x.endswith('.npy'):
                self.files.append(x[:-4])
            else:
                self.files.append(x)
        self.zip = _zip
        self.f = BagObj(self)
        if own_fid:
            self.fid = fid
        else:
            self.fid = None

    def close(self):
        """
        Close the file.

        """
        if self.zip is not None:
            self.zip.close()
            self.zip = None
        if self.fid is not None:
            self.fid.close()
            self.fid = None

    def __del__(self):
        self.close()

    def __getitem__(self, key):
        # FIXME: This seems like it will copy strings around
        #   more than is strictly necessary.  The zipfile
        #   will read the string and then
        #   the format.read_array will copy the string
        #   to another place in memory.
        #   It would be better if the zipfile could read
        #   (or at least uncompress) the data
        #   directly into the array memory.
        member = 0
        if key in self._files:
            member = 1
        elif key in self.files:
            member = 1
            key += '.npy'
        if member:
            bytes = self.zip.read(key)
            if bytes.startswith(format.MAGIC_PREFIX):
                value = BytesIO(bytes)
                return format.read_array(value)
            else:
                return bytes
        else:
            raise KeyError, "%s is not a file in the archive" % key


    def __iter__(self):
        return iter(self.files)

    def items(self):
        """
        Return a list of tuples, with each tuple (filename, array in file).

        """
        return [(f, self[f]) for f in self.files]

    def iteritems(self):
        """Generator that returns tuples (filename, array in file)."""
        for f in self.files:
            yield (f, self[f])

    def keys(self):
        """Return files in the archive with a ".npy" extension."""
        return self.files

    def iterkeys(self):
        """Return an iterator over the files in the archive."""
        return self.__iter__()

    def __contains__(self, key):
        return self.files.__contains__(key)


def load(file, mmap_mode=None):
    """
    Load a pickled, ``.npy``, or ``.npz`` binary file.

    Parameters
    ----------
    file : file-like object or string
        The file to read.  It must support ``seek()`` and ``read()`` methods.
        If the filename extension is ``.gz``, the file is first decompressed.
    mmap_mode: {None, 'r+', 'r', 'w+', 'c'}, optional
        If not None, then memory-map the file, using the given mode
        (see `numpy.memmap`).  The mode has no effect for pickled or
        zipped files.
        A memory-mapped array is stored on disk, and not directly loaded
        into memory.  However, it can be accessed and sliced like any
        ndarray.  Memory mapping is especially useful for accessing
        small fragments of large files without reading the entire file
        into memory.

    Returns
    -------
    result : array, tuple, dict, etc.
        Data stored in the file.

    Raises
    ------
    IOError
        If the input file does not exist or cannot be read.

    See Also
    --------
    save, savez, loadtxt
    memmap : Create a memory-map to an array stored in a file on disk.

    Notes
    -----
    - If the file contains pickle data, then whatever is stored in the
      pickle is returned.
    - If the file is a ``.npy`` file, then an array is returned.
    - If the file is a ``.npz`` file, then a dictionary-like object is
      returned, containing ``{filename: array}`` key-value pairs, one for
      each file in the archive.

    Examples
    --------
    Store data to disk, and load it again:

    >>> np.save('/tmp/123', np.array([[1, 2, 3], [4, 5, 6]]))
    >>> np.load('/tmp/123.npy')
    array([[1, 2, 3],
           [4, 5, 6]])

    Mem-map the stored array, and then access the second row
    directly from disk:

    >>> X = np.load('/tmp/123.npy', mmap_mode='r')
    >>> X[1, :]
    memmap([4, 5, 6])

    """
    import gzip

    own_fid = False
    if isinstance(file, basestring):
        fid = open(file, "rb")
        own_fid = True
    elif isinstance(file, gzip.GzipFile):
        fid = seek_gzip_factory(file)
        own_fid = True
    else:
        fid = file

    try:
        # Code to distinguish from NumPy binary files and pickles.
        _ZIP_PREFIX = asbytes('PK\x03\x04')
        N = len(format.MAGIC_PREFIX)
        magic = fid.read(N)
        fid.seek(-N, 1) # back-up
        if magic.startswith(_ZIP_PREFIX):  # zip-file (assume .npz)
            own_fid = False
            return NpzFile(fid, own_fid=True)
        elif magic == format.MAGIC_PREFIX: # .npy file
            if mmap_mode:
                return format.open_memmap(file, mode=mmap_mode)
            else:
                return format.read_array(fid)
        else:  # Try a pickle
            try:
                return _cload(fid)
            except:
                raise IOError, \
                    "Failed to interpret file %s as a pickle" % repr(file)
    finally:
        if own_fid:
            fid.close()

def save(file, arr):
    """
    Save an array to a binary file in NumPy ``.npy`` format.

    Parameters
    ----------
    file : file or str
        File or filename to which the data is saved.  If file is a file-object,
        then the filename is unchanged.  If file is a string, a ``.npy``
        extension will be appended to the file name if it does not already
        have one.
    arr : array_like
        Array data to be saved.

    See Also
    --------
    savez : Save several arrays into a ``.npz`` archive
    savetxt, load

    Notes
    -----
    For a description of the ``.npy`` format, see `format`.

    Examples
    --------
    >>> from tempfile import TemporaryFile
    >>> outfile = TemporaryFile()

    >>> x = np.arange(10)
    >>> np.save(outfile, x)

    >>> outfile.seek(0) # Only needed here to simulate closing & reopening file
    >>> np.load(outfile)
    array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])

    """
    own_fid = False
    if isinstance(file, basestring):
        if not file.endswith('.npy'):
            file = file + '.npy'
        fid = open(file, "wb")
        own_fid = True
    else:
        fid = file

    try:
        arr = np.asanyarray(arr)
        format.write_array(fid, arr)
    finally:
        if own_fid:
            fid.close()

def savez(file, *args, **kwds):
    """
    Save several arrays into a single file in uncompressed ``.npz`` format.

    If arguments are passed in with no keywords, the corresponding variable
    names, in the .npz file, are 'arr_0', 'arr_1', etc. If keyword arguments
    are given, the corresponding variable names, in the ``.npz`` file will
    match the keyword names.

    Parameters
    ----------
    file : str or file
        Either the file name (string) or an open file (file-like object)
        where the data will be saved. If file is a string, the ``.npz``
        extension will be appended to the file name if it is not already there.
    *args : Arguments, optional
        Arrays to save to the file. Since it is not possible for Python to
        know the names of the arrays outside `savez`, the arrays will be saved
        with names "arr_0", "arr_1", and so on. These arguments can be any
        expression.
    **kwds : Keyword arguments, optional
        Arrays to save to the file. Arrays will be saved in the file with the
        keyword names.

    Returns
    -------
    None

    See Also
    --------
    save : Save a single array to a binary file in NumPy format.
    savetxt : Save an array to a file as plain text.

    Notes
    -----
    The ``.npz`` file format is a zipped archive of files named after the
    variables they contain.  The archive is not compressed and each file
    in the archive contains one variable in ``.npy`` format. For a
    description of the ``.npy`` format, see `format`.

    When opening the saved ``.npz`` file with `load` a `NpzFile` object is
    returned. This is a dictionary-like object which can be queried for
    its list of arrays (with the ``.files`` attribute), and for the arrays
    themselves.

    Examples
    --------
    >>> from tempfile import TemporaryFile
    >>> outfile = TemporaryFile()
    >>> x = np.arange(10)
    >>> y = np.sin(x)

    Using `savez` with *args, the arrays are saved with default names.

    >>> np.savez(outfile, x, y)
    >>> outfile.seek(0) # Only needed here to simulate closing & reopening file
    >>> npzfile = np.load(outfile)
    >>> npzfile.files
    ['arr_1', 'arr_0']
    >>> npzfile['arr_0']
    array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])

    Using `savez` with **kwds, the arrays are saved with the keyword names.

    >>> outfile = TemporaryFile()
    >>> np.savez(outfile, x=x, y=y)
    >>> outfile.seek(0)
    >>> npzfile = np.load(outfile)
    >>> npzfile.files
    ['y', 'x']
    >>> npzfile['x']
    array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])

    See Also
    --------
    numpy.savez_compressed : Save several arrays into a compressed .npz file format

    """
    _savez(file, args, kwds, False)

def savez_compressed(file, *args, **kwds):
    """
    Save several arrays into a single file in compressed ``.npz`` format.

    If keyword arguments are given, then filenames are taken from the keywords.
    If arguments are passed in with no keywords, then stored file names are
    arr_0, arr_1, etc.

    Parameters
    ----------
    file : string
        File name of .npz file.
    args : Arguments
        Function arguments.
    kwds : Keyword arguments
        Keywords.

    See Also
    --------
    numpy.savez : Save several arrays into an uncompressed .npz file format

    """
    _savez(file, args, kwds, True)

def _savez(file, args, kwds, compress):
    # Import is postponed to here since zipfile depends on gzip, an optional
    # component of the so-called standard library.
    import zipfile
    # Import deferred for startup time improvement
    import tempfile

    if isinstance(file, basestring):
        if not file.endswith('.npz'):
            file = file + '.npz'

    namedict = kwds
    for i, val in enumerate(args):
        key = 'arr_%d' % i
        if key in namedict.keys():
            raise ValueError, "Cannot use un-named variables and keyword %s" % key
        namedict[key] = val

    if compress:
        compression = zipfile.ZIP_DEFLATED
    else:
        compression = zipfile.ZIP_STORED

    zip = zipfile_factory(file, mode="w", compression=compression)

    # Stage arrays in a temporary file on disk, before writing to zip.
    fd, tmpfile = tempfile.mkstemp(suffix='-numpy.npy')
    os.close(fd)
    try:
        for key, val in namedict.iteritems():
            fname = key + '.npy'
            fid = open(tmpfile, 'wb')
            try:
                format.write_array(fid, np.asanyarray(val))
                fid.close()
                fid = None
                zip.write(tmpfile, arcname=fname)
            finally:
                if fid:
                    fid.close()
    finally:
        os.remove(tmpfile)

    zip.close()

# Adapted from matplotlib

def _getconv(dtype):
    typ = dtype.type
    if issubclass(typ, np.bool_):
        return lambda x: bool(int(x))
    if issubclass(typ, np.integer):
        return lambda x: int(float(x))
    elif issubclass(typ, np.floating):
        return float
    elif issubclass(typ, np.complex):
        return complex
    elif issubclass(typ, np.bytes_):
        return bytes
    else:
        return str



def loadtxt(fname, dtype=float, comments='#', delimiter=None,
            converters=None, skiprows=0, usecols=None, unpack=False):
    """
    Load data from a text file.

    Each row in the text file must have the same number of values.

    Parameters
    ----------
    fname : file or str
        File or filename to read.  If the filename extension is ``.gz`` or
        ``.bz2``, the file is first decompressed.
    dtype : data-type, optional
        Data-type of the resulting array; default: float.  If this is a record
        data-type, the resulting array will be 1-dimensional, and each row
        will be interpreted as an element of the array.  In this case, the
        number of columns used must match the number of fields in the
        data-type.
    comments : str, optional
        The character used to indicate the start of a comment; default: '#'.
    delimiter : str, optional
        The string used to separate values.  By default, this is any
        whitespace.
    converters : dict, optional
        A dictionary mapping column number to a function that will convert
        that column to a float.  E.g., if column 0 is a date string:
        ``converters = {0: datestr2num}``.  Converters can also be used to
        provide a default value for missing data:
        ``converters = {3: lambda s: float(s or 0)}``.  Default: None.
    skiprows : int, optional
        Skip the first `skiprows` lines; default: 0.
    usecols : sequence, optional
        Which columns to read, with 0 being the first.  For example,
        ``usecols = (1,4,5)`` will extract the 2nd, 5th and 6th columns.
        The default, None, results in all columns being read.
    unpack : bool, optional
        If True, the returned array is transposed, so that arguments may be
        unpacked using ``x, y, z = loadtxt(...)``.  The default is False.

    Returns
    -------
    out : ndarray
        Data read from the text file.

    See Also
    --------
    load, fromstring, fromregex
    genfromtxt : Load data with missing values handled as specified.
    scipy.io.loadmat : reads MATLAB data files

    Notes
    -----
    This function aims to be a fast reader for simply formatted files.  The
    `genfromtxt` function provides more sophisticated handling of, e.g.,
    lines with missing values.

    Examples
    --------
    >>> from StringIO import StringIO   # StringIO behaves like a file object
    >>> c = StringIO("0 1\\n2 3")
    >>> np.loadtxt(c)
    array([[ 0.,  1.],
           [ 2.,  3.]])

    >>> d = StringIO("M 21 72\\nF 35 58")
    >>> np.loadtxt(d, dtype={'names': ('gender', 'age', 'weight'),
    ...                      'formats': ('S1', 'i4', 'f4')})
    array([('M', 21, 72.0), ('F', 35, 58.0)],
          dtype=[('gender', '|S1'), ('age', '<i4'), ('weight', '<f4')])

    >>> c = StringIO("1,0,2\\n3,0,4")
    >>> x, y = np.loadtxt(c, delimiter=',', usecols=(0, 2), unpack=True)
    >>> x
    array([ 1.,  3.])
    >>> y
    array([ 2.,  4.])

    """
    # Type conversions for Py3 convenience
    comments = asbytes(comments)
    if delimiter is not None:
        delimiter = asbytes(delimiter)

    user_converters = converters

    if usecols is not None:
        usecols = list(usecols)

    own_fh = False
    if _is_string_like(fname):
        own_fh = True
        if fname.endswith('.gz'):
            fh = seek_gzip_factory(fname)
        elif fname.endswith('.bz2'):
            import bz2
            fh = bz2.BZ2File(fname)
        else:
            fh = open(fname, 'U')
    elif hasattr(fname, 'readline'):
        fh = fname
    else:
        raise ValueError('fname must be a string or file handle')

    def flatten_dtype(dt):
        """Unpack a structured data-type."""
        if dt.names is None:
            # If the dtype is flattened, return.
            # If the dtype has a shape, the dtype occurs
            # in the list more than once.
            return [dt.base] * int(np.prod(dt.shape))
        else:
            types = []
            for field in dt.names:
                tp, bytes = dt.fields[field]
                flat_dt = flatten_dtype(tp)
                types.extend(flat_dt)
            return types

    def split_line(line):
        """Chop off comments, strip, and split at delimiter."""
        line = asbytes(line).split(comments)[0].strip()
        if line:
            return line.split(delimiter)
        else:
            return []

    try:
        # Make sure we're dealing with a proper dtype
        dtype = np.dtype(dtype)
        defconv = _getconv(dtype)

        # Skip the first `skiprows` lines
        for i in xrange(skiprows):
            fh.readline()

        # Read until we find a line with some values, and use it to
        # estimate the number of columns N. Then continue until end of
        # file to estimate the number of rows M.
        offset = fh.tell()
        M = 0
        for line in fh:
            if not line:
                raise IOError('End-of-file reached before encountering data.')
            first_vals = split_line(line)
            M = M + 1
            if first_vals: break
        for line in fh:
            # continue till end of file
            M += 1
        fh.seek(offset)

        N = len(usecols or first_vals)

        dtype_types = flatten_dtype(dtype)
        if len(dtype_types) > 1:
            # We're dealing with a structured array, each field of
            # the dtype matches a column
            converters = [_getconv(dt) for dt in dtype_types]
        else:
            # All fields have the same dtype
            converters = [defconv for i in xrange(N)]

        # By preference, use the converters specified by the user
        for i, conv in (user_converters or {}).iteritems():
            if usecols:
                try:
                    i = usecols.index(i)
                except ValueError:
                    # Unused converter specified
                    continue
            converters[i] = conv

        X = np.empty((M, N), dtype=dtype)

        # Parse each line, including the first
        for i, line in enumerate(fh):
            vals = split_line(line)
            if len(vals) == 0:
                continue

            if usecols:
                vals = [vals[i] for i in usecols]

            # Convert each value according to its column and store
            X[i] = [conv(val) for (conv, val) in zip(converters, vals)]
    finally:
        if own_fh:
            fh.close()

    if len(dtype_types) > 1:
        # We're dealing with a structured array, with a dtype such as
        # [('x', int), ('y', [('s', int), ('t', float)])]
        #
        # First, create the array using a flattened dtype:
        # [('x', int), ('s', int), ('t', float)]
        #
        # Then, view the array using the specified dtype.
        try:
            X = np.array(X, dtype=np.dtype([('', t) for t in dtype_types]))
            X = X.view(dtype)
        except TypeError:
            # In the case we have an object dtype
            X = np.array(X, dtype=dtype)
    else:
        X = np.array(X, dtype)

    X = np.squeeze(X)
    if unpack:
        return X.T
    else:
        return X


def savetxt(fname, X, fmt='%.18e', delimiter=' ', newline='\n'):
    """
    Save an array to a text file.

    Parameters
    ----------
    fname : filename or file handle
        If the filename ends in ``.gz``, the file is automatically saved in
        compressed gzip format.  `loadtxt` understands gzipped files
        transparently.
    X : array_like
        Data to be saved to a text file.
    fmt : str or sequence of strs
        A single format (%10.5f), a sequence of formats, or a
        multi-format string, e.g. 'Iteration %d -- %10.5f', in which
        case `delimiter` is ignored.
    delimiter : str
        Character separating columns.
    newline : str
        .. versionadded:: 1.5.0

        Character separating lines.


    See Also
    --------
    save : Save an array to a binary file in NumPy ``.npy`` format
    savez : Save several arrays into a ``.npz`` compressed archive

    Notes
    -----
    Further explanation of the `fmt` parameter
    (``%[flag]width[.precision]specifier``):

    flags:
        ``-`` : left justify

        ``+`` : Forces to preceed result with + or -.

        ``0`` : Left pad the number with zeros instead of space (see width).

    width:
        Minimum number of characters to be printed. The value is not truncated
        if it has more characters.

    precision:
        - For integer specifiers (eg. ``d,i,o,x``), the minimum number of
          digits.
        - For ``e, E`` and ``f`` specifiers, the number of digits to print
          after the decimal point.
        - For ``g`` and ``G``, the maximum number of significant digits.
        - For ``s``, the maximum number of characters.

    specifiers:
        ``c`` : character

        ``d`` or ``i`` : signed decimal integer

        ``e`` or ``E`` : scientific notation with ``e`` or ``E``.

        ``f`` : decimal floating point

        ``g,G`` : use the shorter of ``e,E`` or ``f``

        ``o`` : signed octal

        ``s`` : string of characters

        ``u`` : unsigned decimal integer

        ``x,X`` : unsigned hexadecimal integer

    This explanation of ``fmt`` is not complete, for an exhaustive
    specification see [1]_.

    References
    ----------
    .. [1] `Format Specification Mini-Language
           <http://docs.python.org/library/string.html#
           format-specification-mini-language>`_, Python Documentation.

    Examples
    --------
    >>> x = y = z = np.arange(0.0,5.0,1.0)
    >>> np.savetxt('test.out', x, delimiter=',')   # X is an array
    >>> np.savetxt('test.out', (x,y,z))   # x,y,z equal sized 1D arrays
    >>> np.savetxt('test.out', x, fmt='%1.4e')   # use exponential notation

    """

    # Py3 conversions first
    if isinstance(fmt, bytes):
        fmt = asstr(fmt)
    delimiter = asstr(delimiter)

    own_fh = False
    if _is_string_like(fname):
        own_fh = True
        if fname.endswith('.gz'):
            import gzip
            fh = gzip.open(fname, 'wb')
        else:
            if sys.version_info[0] >= 3:
                fh = open(fname, 'wb')
            else:
                fh = open(fname, 'w')
    elif hasattr(fname, 'seek'):
        fh = fname
    else:
        raise ValueError('fname must be a string or file handle')

    try:
        X = np.asarray(X)

        # Handle 1-dimensional arrays
        if X.ndim == 1:
            # Common case -- 1d array of numbers
            if X.dtype.names is None:
                X = np.atleast_2d(X).T
                ncol = 1

            # Complex dtype -- each field indicates a separate column
            else:
                ncol = len(X.dtype.descr)
        else:
            ncol = X.shape[1]

        # `fmt` can be a string with multiple insertion points or a
        # list of formats.  E.g. '%10.5f\t%10d' or ('%10.5f', '$10d')
        if type(fmt) in (list, tuple):
            if len(fmt) != ncol:
                raise AttributeError('fmt has wrong shape.  %s' % str(fmt))
            format = asstr(delimiter).join(map(asstr, fmt))
        elif type(fmt) is str:
            if fmt.count('%') == 1:
                fmt = [fmt, ]*ncol
                format = delimiter.join(fmt)
            elif fmt.count('%') != ncol:
                raise AttributeError('fmt has wrong number of %% formats.  %s'
                                     % fmt)
            else:
                format = fmt

        for row in X:
            fh.write(asbytes(format % tuple(row) + newline))
    finally:
        if own_fh:
            fh.close()

import re
def fromregex(file, regexp, dtype):
    """
    Construct an array from a text file, using regular expression parsing.

    The returned array is always a structured array, and is constructed from
    all matches of the regular expression in the file. Groups in the regular
    expression are converted to fields of the structured array.

    Parameters
    ----------
    file : str or file
        File name or file object to read.
    regexp : str or regexp
        Regular expression used to parse the file.
        Groups in the regular expression correspond to fields in the dtype.
    dtype : dtype or list of dtypes
        Dtype for the structured array.

    Returns
    -------
    output : ndarray
        The output array, containing the part of the content of `file` that
        was matched by `regexp`. `output` is always a structured array.

    Raises
    ------
    TypeError
        When `dtype` is not a valid dtype for a structured array.

    See Also
    --------
    fromstring, loadtxt

    Notes
    -----
    Dtypes for structured arrays can be specified in several forms, but all
    forms specify at least the data type and field name. For details see
    `doc.structured_arrays`.

    Examples
    --------
    >>> f = open('test.dat', 'w')
    >>> f.write("1312 foo\\n1534  bar\\n444   qux")
    >>> f.close()

    >>> regexp = r"(\\d+)\\s+(...)"  # match [digits, whitespace, anything]
    >>> output = np.fromregex('test.dat', regexp,
    ...                       [('num', np.int64), ('key', 'S3')])
    >>> output
    array([(1312L, 'foo'), (1534L, 'bar'), (444L, 'qux')],
          dtype=[('num', '<i8'), ('key', '|S3')])
    >>> output['num']
    array([1312, 1534,  444], dtype=int64)

    """
    own_fh = False
    if not hasattr(file, "read"):
        file = open(file, 'rb')
        own_fh = True

    try:
        if not hasattr(regexp, 'match'):
            regexp = re.compile(asbytes(regexp))
        if not isinstance(dtype, np.dtype):
            dtype = np.dtype(dtype)

        seq = regexp.findall(file.read())
        if seq and not isinstance(seq[0], tuple):
            # Only one group is in the regexp.
            # Create the new array as a single data-type and then
            #   re-interpret as a single-field structured array.
            newdtype = np.dtype(dtype[dtype.names[0]])
            output = np.array(seq, dtype=newdtype)
            output.dtype = dtype
        else:
            output = np.array(seq, dtype=dtype)

        return output
    finally:
        if own_fh:
            fh.close()




#####--------------------------------------------------------------------------
#---- --- ASCII functions ---
#####--------------------------------------------------------------------------



def genfromtxt(fname, dtype=float, comments='#', delimiter=None,
               skiprows=0, skip_header=0, skip_footer=0, converters=None,
               missing='', missing_values=None, filling_values=None,
               usecols=None, names=None,
               excludelist=None, deletechars=None, replace_space='_',
               autostrip=False, case_sensitive=True, defaultfmt="f%i",
               unpack=None, usemask=False, loose=True, invalid_raise=True):
    """
    Load data from a text file, with missing values handled as specified.

    Each line past the first `skiprows` lines is split at the `delimiter`
    character, and characters following the `comments` character are discarded.

    Parameters
    ----------
    fname : file or str
        File or filename to read.  If the filename extension is `.gz` or
        `.bz2`, the file is first decompressed.
    dtype : dtype, optional
        Data type of the resulting array.
        If None, the dtypes will be determined by the contents of each
        column, individually.
    comments : str, optional
        The character used to indicate the start of a comment.
        All the characters occurring on a line after a comment are discarded
    delimiter : str, int, or sequence, optional
        The string used to separate values.  By default, any consecutive
        whitespaces act as delimiter.  An integer or sequence of integers
        can also be provided as width(s) of each field.
    skip_header : int, optional
        The numbers of lines to skip at the beginning of the file.
    skip_footer : int, optional
        The numbers of lines to skip at the end of the file
    converters : variable or None, optional
        The set of functions that convert the data of a column to a value.
        The converters can also be used to provide a default value
        for missing data: ``converters = {3: lambda s: float(s or 0)}``.
    missing_values : variable or None, optional
        The set of strings corresponding to missing data.
    filling_values : variable or None, optional
        The set of values to be used as default when the data are missing.
    usecols : sequence or None, optional
        Which columns to read, with 0 being the first.  For example,
        ``usecols = (1, 4, 5)`` will extract the 2nd, 5th and 6th columns.
    names : {None, True, str, sequence}, optional
        If `names` is True, the field names are read from the first valid line
        after the first `skiprows` lines.
        If `names` is a sequence or a single-string of comma-separated names,
        the names will be used to define the field names in a structured dtype.
        If `names` is None, the names of the dtype fields will be used, if any.
    excludelist : sequence, optional
        A list of names to exclude. This list is appended to the default list
        ['return','file','print']. Excluded names are appended an underscore:
        for example, `file` would become `file_`.
    deletechars : str, optional
        A string combining invalid characters that must be deleted from the
        names.
    defaultfmt : str, optional
        A format used to define default field names, such as "f%i" or "f_%02i".
    autostrip : bool, optional
        Whether to automatically strip white spaces from the variables.
    replace_space : char, optional
        Character(s) used in replacement of white spaces in the variables names.
        By default, use a '_'.
    case_sensitive : {True, False, 'upper', 'lower'}, optional
        If True, field names are case sensitive.
        If False or 'upper', field names are converted to upper case.
        If 'lower', field names are converted to lower case.
    unpack : bool, optional
        If True, the returned array is transposed, so that arguments may be
        unpacked using ``x, y, z = loadtxt(...)``
    usemask : bool, optional
        If True, return a masked array.
        If False, return a regular array.
    invalid_raise : bool, optional
        If True, an exception is raised if an inconsistency is detected in the
        number of columns.
        If False, a warning is emitted and the offending lines are skipped.

    Returns
    -------
    out : ndarray
        Data read from the text file. If `usemask` is True, this is a
        masked array.

    See Also
    --------
    numpy.loadtxt : equivalent function when no data is missing.

    Notes
    -----
    * When spaces are used as delimiters, or when no delimiter has been given
      as input, there should not be any missing data between two fields.
    * When the variables are named (either by a flexible dtype or with `names`,
      there must not be any header in the file (else a ValueError
      exception is raised).
    * Individual values are not stripped of spaces by default.
      When using a custom converter, make sure the function does remove spaces.

    Examples
    ---------
    >>> from StringIO import StringIO
    >>> import numpy as np

    Comma delimited file with mixed dtype

    >>> s = StringIO("1,1.3,abcde")
    >>> data = np.genfromtxt(s, dtype=[('myint','i8'),('myfloat','f8'),
    ... ('mystring','S5')], delimiter=",")
    >>> data
    array((1, 1.3, 'abcde'),
          dtype=[('myint', '<i8'), ('myfloat', '<f8'), ('mystring', '|S5')])

    Using dtype = None

    >>> s.seek(0) # needed for StringIO example only
    >>> data = np.genfromtxt(s, dtype=None,
    ... names = ['myint','myfloat','mystring'], delimiter=",")
    >>> data
    array((1, 1.3, 'abcde'),
          dtype=[('myint', '<i8'), ('myfloat', '<f8'), ('mystring', '|S5')])

    Specifying dtype and names

    >>> s.seek(0)
    >>> data = np.genfromtxt(s, dtype="i8,f8,S5",
    ... names=['myint','myfloat','mystring'], delimiter=",")
    >>> data
    array((1, 1.3, 'abcde'),
          dtype=[('myint', '<i8'), ('myfloat', '<f8'), ('mystring', '|S5')])

    An example with fixed-width columns

    >>> s = StringIO("11.3abcde")
    >>> data = np.genfromtxt(s, dtype=None, names=['intvar','fltvar','strvar'],
    ...     delimiter=[1,3,5])
    >>> data
    array((1, 1.3, 'abcde'),
          dtype=[('intvar', '<i8'), ('fltvar', '<f8'), ('strvar', '|S5')])

    """
    # Py3 data conversions to bytes, for convenience
    comments = asbytes(comments)
    if isinstance(delimiter, unicode):
        delimiter = asbytes(delimiter)
    if isinstance(missing, unicode):
        missing = asbytes(missing)
    if isinstance(missing_values, (unicode, list, tuple)):
        missing_values = asbytes_nested(missing_values)

    #
    if usemask:
        from numpy.ma import MaskedArray, make_mask_descr
    # Check the input dictionary of converters
    user_converters = converters or {}
    if not isinstance(user_converters, dict):
        errmsg = "The input argument 'converter' should be a valid dictionary "\
            "(got '%s' instead)"
        raise TypeError(errmsg % type(user_converters))

    # Initialize the filehandle, the LineSplitter and the NameValidator
    own_fhd = False
    if isinstance(fname, basestring):
        fhd = np.lib._datasource.open(fname, 'U')
        own_fhd = True
    elif not hasattr(fname, 'read'):
        raise TypeError("The input should be a string or a filehandle. "\
                        "(got %s instead)" % type(fname))
    else:
        fhd = fname
    split_line = LineSplitter(delimiter=delimiter, comments=comments,
                              autostrip=autostrip)._handyman
    validate_names = NameValidator(excludelist=excludelist,
                                   deletechars=deletechars,
                                   case_sensitive=case_sensitive,
                                   replace_space=replace_space)

    # Get the first valid lines after the first skiprows ones ..
    if skiprows:
        warnings.warn("The use of `skiprows` is deprecated.\n"\
                      "Please use `skip_header` instead.",
                      DeprecationWarning)
        skip_header = skiprows
    # Skip the first `skip_header` rows
    for i in xrange(skip_header):
        fhd.readline()
    # Keep on until we find the first valid values
    first_values = None
    while not first_values:
        first_line = fhd.readline()
        if not first_line:
            raise IOError('End-of-file reached before encountering data.')
        if names is True:
            if comments in first_line:
                first_line = asbytes('').join(first_line.split(comments)[1:])
        first_values = split_line(first_line)
    # Should we take the first values as names ?
    if names is True:
        fval = first_values[0].strip()
        if fval in comments:
            del first_values[0]

    # Check the columns to use: make sure `usecols` is a list
    if usecols is not None:
        try:
            usecols = [_.strip() for _ in usecols.split(",")]
        except AttributeError:
            try:
                usecols = list(usecols)
            except TypeError:
                usecols = [usecols, ]
    nbcols = len(usecols or first_values)

    # Check the names and overwrite the dtype.names if needed
    if names is True:
        names = validate_names([_bytes_to_name(_.strip())
                                for _ in first_values])
        first_line = asbytes('')
    elif _is_string_like(names):
        names = validate_names([_.strip() for _ in names.split(',')])
    elif names:
        names = validate_names(names)
    # Get the dtype
    if dtype is not None:
        dtype = easy_dtype(dtype, defaultfmt=defaultfmt, names=names)
    # Make sure the names is a list (for 2.5)
    if names is not None:
        names = list(names)


    if usecols:
        for (i, current) in enumerate(usecols):
            # if usecols is a list of names, convert to a list of indices
            if _is_string_like(current):
                usecols[i] = names.index(current)
            elif current < 0:
                usecols[i] = current + len(first_values)
        # If the dtype is not None, make sure we update it
        if (dtype is not None) and (len(dtype) > nbcols):
            descr = dtype.descr
            dtype = np.dtype([descr[_] for _ in usecols])
            names = list(dtype.names)
        # If `names` is not None, update the names
        elif (names is not None) and (len(names) > nbcols):
            names = [names[_] for _ in usecols]
    elif (names is not None) and (dtype is not None):
        names = dtype.names


    # Process the missing values ...............................
    # Rename missing_values for convenience
    user_missing_values = missing_values or ()

    # Define the list of missing_values (one column: one list)
    missing_values = [list([asbytes('')]) for _ in range(nbcols)]

    # We have a dictionary: process it field by field
    if isinstance(user_missing_values, dict):
        # Loop on the items
        for (key, val) in user_missing_values.items():
            # Is the key a string ?
            if _is_string_like(key):
                try:
                    # Transform it into an integer
                    key = names.index(key)
                except ValueError:
                    # We couldn't find it: the name must have been dropped, then
                    continue
            # Redefine the key as needed if it's a column number
            if usecols:
                try:
                    key = usecols.index(key)
                except ValueError:
                    pass
            # Transform the value as a list of string
            if isinstance(val, (list, tuple)):
                val = [str(_) for _ in val]
            else:
                val = [str(val), ]
            # Add the value(s) to the current list of missing
            if key is None:
                # None acts as default
                for miss in missing_values:
                    miss.extend(val)
            else:
                missing_values[key].extend(val)
    # We have a sequence : each item matches a column
    elif isinstance(user_missing_values, (list, tuple)):
        for (value, entry) in zip(user_missing_values, missing_values):
            value = str(value)
            if value not in entry:
                entry.append(value)
    # We have a string : apply it to all entries
    elif isinstance(user_missing_values, bytes):
        user_value = user_missing_values.split(asbytes(","))
        for entry in missing_values:
            entry.extend(user_value)
    # We have something else: apply it to all entries
    else:
        for entry in missing_values:
            entry.extend([str(user_missing_values)])

    # Process the deprecated `missing`
    if missing != asbytes(''):
        warnings.warn("The use of `missing` is deprecated.\n"\
                      "Please use `missing_values` instead.",
                      DeprecationWarning)
        values = [str(_) for _ in missing.split(asbytes(","))]
        for entry in missing_values:
            entry.extend(values)

    # Process the filling_values ...............................
    # Rename the input for convenience
    user_filling_values = filling_values or []
    # Define the default
    filling_values = [None] * nbcols
    # We have a dictionary : update each entry individually
    if isinstance(user_filling_values, dict):
        for (key, val) in user_filling_values.items():
            if _is_string_like(key):
                try:
                    # Transform it into an integer
                    key = names.index(key)
                except ValueError:
                    # We couldn't find it: the name must have been dropped, then
                    continue
            # Redefine the key if it's a column number and usecols is defined
            if usecols:
                try:
                    key = usecols.index(key)
                except ValueError:
                    pass
            # Add the value to the list
            filling_values[key] = val
    # We have a sequence : update on a one-to-one basis
    elif isinstance(user_filling_values, (list, tuple)):
        n = len(user_filling_values)
        if (n <= nbcols):
            filling_values[:n] = user_filling_values
        else:
            filling_values = user_filling_values[:nbcols]
    # We have something else : use it for all entries
    else:
        filling_values = [user_filling_values] * nbcols

    # Initialize the converters ................................
    if dtype is None:
        # Note: we can't use a [...]*nbcols, as we would have 3 times the same
        # ... converter, instead of 3 different converters.
        converters = [StringConverter(None, missing_values=miss, default=fill)
                      for (miss, fill) in zip(missing_values, filling_values)]
    else:
        dtype_flat = flatten_dtype(dtype, flatten_base=True)
        # Initialize the converters
        if len(dtype_flat) > 1:
            # Flexible type : get a converter from each dtype
            zipit = zip(dtype_flat, missing_values, filling_values)
            converters = [StringConverter(dt, locked=True,
                                          missing_values=miss, default=fill)
                           for (dt, miss, fill) in zipit]
        else:
            # Set to a default converter (but w/ different missing values)
            zipit = zip(missing_values, filling_values)
            converters = [StringConverter(dtype, locked=True,
                                          missing_values=miss, default=fill)
                          for (miss, fill) in zipit]
    # Update the converters to use the user-defined ones
    uc_update = []
    for (i, conv) in user_converters.items():
        # If the converter is specified by column names, use the index instead
        if _is_string_like(i):
            try:
                i = names.index(i)
            except ValueError:
                continue
        elif usecols:
            try:
                i = usecols.index(i)
            except ValueError:
                # Unused converter specified
                continue
        # Find the value to test:
        if len(first_line):
            testing_value = first_values[i]
        else:
            testing_value = None
        converters[i].update(conv, locked=True,
                             testing_value=testing_value,
                             default=filling_values[i],
                             missing_values=missing_values[i],)
        uc_update.append((i, conv))
    # Make sure we have the corrected keys in user_converters...
    user_converters.update(uc_update)

    miss_chars = [_.missing_values for _ in converters]


    # Initialize the output lists ...
    # ... rows
    rows = []
    append_to_rows = rows.append
    # ... masks
    if usemask:
        masks = []
        append_to_masks = masks.append
    # ... invalid
    invalid = []
    append_to_invalid = invalid.append

    # Parse each line
    for (i, line) in enumerate(itertools.chain([first_line, ], fhd)):
        values = split_line(line)
        nbvalues = len(values)
        # Skip an empty line
        if nbvalues == 0:
            continue
        # Select only the columns we need
        if usecols:
            try:
                values = [values[_] for _ in usecols]
            except IndexError:
                append_to_invalid((i + skip_header + 1, nbvalues))
                continue
        elif nbvalues != nbcols:
            append_to_invalid((i + skip_header + 1, nbvalues))
            continue
        # Store the values
        append_to_rows(tuple(values))
        if usemask:
            append_to_masks(tuple([v.strip() in m
                                   for (v, m) in zip(values, missing_values)]))

    if own_fhd:
        fhd.close()

    # Upgrade the converters (if needed)
    if dtype is None:
        for (i, converter) in enumerate(converters):
            current_column = map(itemgetter(i), rows)
            try:
                converter.iterupgrade(current_column)
            except ConverterLockError:
                errmsg = "Converter #%i is locked and cannot be upgraded: " % i
                current_column = itertools.imap(itemgetter(i), rows)
                for (j, value) in enumerate(current_column):
                    try:
                        converter.upgrade(value)
                    except (ConverterError, ValueError):
                        errmsg += "(occurred line #%i for value '%s')"
                        errmsg %= (j + 1 + skip_header, value)
                        raise ConverterError(errmsg)

    # Check that we don't have invalid values
    nbinvalid = len(invalid)
    if nbinvalid > 0:
        nbrows = len(rows) + nbinvalid - skip_footer
        # Construct the error message
        template = "    Line #%%i (got %%i columns instead of %i)" % nbcols
        if skip_footer > 0:
            nbinvalid_skipped = len([_ for _ in invalid
                                     if _[0] > nbrows + skip_header])
            invalid = invalid[:nbinvalid - nbinvalid_skipped]
            skip_footer -= nbinvalid_skipped
#            
#            nbrows -= skip_footer
#            errmsg = [template % (i, nb)
#                      for (i, nb) in invalid if i < nbrows]
#        else:
        errmsg = [template % (i, nb)
                  for (i, nb) in invalid]
        if len(errmsg):
            errmsg.insert(0, "Some errors were detected !")
            errmsg = "\n".join(errmsg)
            # Raise an exception ?
            if invalid_raise:
                raise ValueError(errmsg)
            # Issue a warning ?
            else:
                warnings.warn(errmsg, ConversionWarning)

    # Strip the last skip_footer data
    if skip_footer > 0:
        rows = rows[:-skip_footer]
        if usemask:
            masks = masks[:-skip_footer]


    # Convert each value according to the converter:
    # We want to modify the list in place to avoid creating a new one...
#    if loose:
#        conversionfuncs = [conv._loose_call for conv in converters]
#    else:
#        conversionfuncs = [conv._strict_call for conv in converters]
#    for (i, vals) in enumerate(rows):
#        rows[i] = tuple([convert(val)
#                         for (convert, val) in zip(conversionfuncs, vals)])
    if loose:
        rows = zip(*[map(converter._loose_call, map(itemgetter(i), rows))
                     for (i, converter) in enumerate(converters)])
    else:
        rows = zip(*[map(converter._strict_call, map(itemgetter(i), rows))
                     for (i, converter) in enumerate(converters)])
    # Reset the dtype
    data = rows
    if dtype is None:
        # Get the dtypes from the types of the converters
        column_types = [conv.type for conv in converters]
        # Find the columns with strings...
        strcolidx = [i for (i, v) in enumerate(column_types)
                     if v in (type('S'), np.string_)]
        # ... and take the largest number of chars.
        for i in strcolidx:
            column_types[i] = "|S%i" % max(len(row[i]) for row in data)
        #
        if names is None:
            # If the dtype is uniform, don't define names, else use ''
            base = set([c.type for c in converters if c._checked])
            if len(base) == 1:
                (ddtype, mdtype) = (list(base)[0], np.bool)
            else:
                ddtype = [(defaultfmt % i, dt)
                          for (i, dt) in enumerate(column_types)]
                if usemask:
                    mdtype = [(defaultfmt % i, np.bool)
                              for (i, dt) in enumerate(column_types)]
        else:
            ddtype = zip(names, column_types)
            mdtype = zip(names, [np.bool] * len(column_types))
        output = np.array(data, dtype=ddtype)
        if usemask:
            outputmask = np.array(masks, dtype=mdtype)
    else:
        # Overwrite the initial dtype names if needed
        if names and dtype.names:
            dtype.names = names
        # Case 1. We have a structured type
        if len(dtype_flat) > 1:
            # Nested dtype, eg  [('a', int), ('b', [('b0', int), ('b1', 'f4')])]
            # First, create the array using a flattened dtype:
            # [('a', int), ('b1', int), ('b2', float)]
            # Then, view the array using the specified dtype.
            if 'O' in (_.char for _ in dtype_flat):
                if has_nested_fields(dtype):
                    errmsg = "Nested fields involving objects "\
                             "are not supported..."
                    raise NotImplementedError(errmsg)
                else:
                    output = np.array(data, dtype=dtype)
            else:
                rows = np.array(data, dtype=[('', _) for _ in dtype_flat])
                output = rows.view(dtype)
            # Now, process the rowmasks the same way
            if usemask:
                rowmasks = np.array(masks,
                                    dtype=np.dtype([('', np.bool)
                                    for t in dtype_flat]))
                # Construct the new dtype
                mdtype = make_mask_descr(dtype)
                outputmask = rowmasks.view(mdtype)
        # Case #2. We have a basic dtype
        else:
            # We used some user-defined converters
            if user_converters:
                ishomogeneous = True
                descr = []
                for (i, ttype) in enumerate([conv.type for conv in converters]):
                    # Keep the dtype of the current converter
                    if i in user_converters:
                        ishomogeneous &= (ttype == dtype.type)
                        if ttype == np.string_:
                            ttype = "|S%i" % max(len(row[i]) for row in data)
                        descr.append(('', ttype))
                    else:
                        descr.append(('', dtype))
                # So we changed the dtype ?
                if not ishomogeneous:
                    # We have more than one field
                    if len(descr) > 1:
                        dtype = np.dtype(descr)
                    # We have only one field: drop the name if not needed.
                    else:
                        dtype = np.dtype(ttype)
            #
            output = np.array(data, dtype)
            if usemask:
                if dtype.names:
                    mdtype = [(_, np.bool) for _ in dtype.names]
                else:
                    mdtype = np.bool
                outputmask = np.array(masks, dtype=mdtype)
    # Try to take care of the missing data we missed
    names = output.dtype.names
    if usemask and names:
        for (name, conv) in zip(names or (), converters):
            missing_values = [conv(_) for _ in conv.missing_values
                              if _ != asbytes('')]
            for mval in missing_values:
                outputmask[name] |= (output[name] == mval)
    # Construct the final array
    if usemask:
        output = output.view(MaskedArray)
        output._mask = outputmask
    if unpack:
        return output.squeeze().T
    return output.squeeze()



def ndfromtxt(fname, **kwargs):
    """
    Load ASCII data stored in a file and return it as a single array.

    Complete description of all the optional input parameters is available in
    the docstring of the `genfromtxt` function.

    See Also
    --------
    numpy.genfromtxt : generic function.

    """
    kwargs['usemask'] = False
    return genfromtxt(fname, **kwargs)


def mafromtxt(fname, **kwargs):
    """
    Load ASCII data stored in a text file and return a masked array.

    For a complete description of all the input parameters, see `genfromtxt`.

    See Also
    --------
    numpy.genfromtxt : generic function to load ASCII data.

    """
    kwargs['usemask'] = True
    return genfromtxt(fname, **kwargs)


def recfromtxt(fname, **kwargs):
    """
    Load ASCII data from a file and return it in a record array.

    If ``usemask=False`` a standard `recarray` is returned,
    if ``usemask=True`` a MaskedRecords array is returned.

    Complete description of all the optional input parameters is available in
    the docstring of the `genfromtxt` function.

    See Also
    --------
    numpy.genfromtxt : generic function

    Notes
    -----
    By default, `dtype` is None, which means that the data-type of the output
    array will be determined from the data.

    """
    kwargs.update(dtype=kwargs.get('dtype', None))
    usemask = kwargs.get('usemask', False)
    output = genfromtxt(fname, **kwargs)
    if usemask:
        from numpy.ma.mrecords import MaskedRecords
        output = output.view(MaskedRecords)
    else:
        output = output.view(np.recarray)
    return output


def recfromcsv(fname, **kwargs):
    """
    Load ASCII data stored in a comma-separated file.

    The returned array is a record array (if ``usemask=False``, see
    `recarray`) or a masked record array (if ``usemask=True``,
    see `ma.mrecords.MaskedRecords`).

    For a complete description of all the input parameters, see `genfromtxt`.

    See Also
    --------
    numpy.genfromtxt : generic function to load ASCII data.

    """
    case_sensitive = kwargs.get('case_sensitive', "lower") or "lower"
    names = kwargs.get('names', True)
    if names is None:
        names = True
    kwargs.update(dtype=kwargs.get('update', None),
                  delimiter=kwargs.get('delimiter', ",") or ",",
                  names=names,
                  case_sensitive=case_sensitive)
    usemask = kwargs.get("usemask", False)
    output = genfromtxt(fname, **kwargs)
    if usemask:
        from numpy.ma.mrecords import MaskedRecords
        output = output.view(MaskedRecords)
    else:
        output = output.view(np.recarray)
    return output
