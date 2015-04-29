#!/usr/bin/env python
#-----------------------------------------------------------------------------
# Copyright (c) 2013, NeXpy Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING, distributed with this software.
#-----------------------------------------------------------------------------

import datetime
import getopt
import glob
import os
import re
import socket
import subprocess
import sys
import time
import timeit
from ConfigParser import ConfigParser
from datetime import datetime

import numpy as np

from nexusformat.nexus import *

prefix_pattern = re.compile('^([^.]+)(?:(?<!\d)|(?=_))')
index_pattern = re.compile('^(.*?)([0-9]*)[.](.*)$')

def natural_sort(key):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', key)]

def get_prefixes(directory):
    prefixes = []
    print 'globbing for prefixes in:', directory
    for filename in os.listdir(directory):
        f = filename.split(os.path.extsep)[0]
        match = prefix_pattern.match(f)
        if match:
            prefixes.append(match.group(1).strip('-').strip('_'))
    return list(set(prefixes))


def get_files(directory, prefix, extension, first=None, last=None,
              reverse=False):
    if not extension.startswith('.'):
        extension = '.' + extension
    filenames = sorted(glob.glob(os.path.join(directory, prefix+'*'+extension)),
                       key=natural_sort, reverse=reverse)
    if len(filenames) == 0:
        print "No filenames matched!"
        exit(0)
    max_index = get_index(filenames[-1])
    if first:
        min_index = first
    else:
        min_index = get_index(filenames[0])
    if last:
        max_index = last
    else:
        max_index = get_index(filenames[-1])
    return [filename for filename in filenames if 
                min_index <= get_index(filename) <= max_index]


def get_index(filename):
    return int(index_pattern.match(filename).group(2))


def read_image(filename):
    if os.path.splitext(filename)[1] == '.cbf':
        try:
            import pycbf
        except ImportError:
            raise NeXusError('Reading CBF files requires the pycbf module')
        cbf = pycbf.cbf_handle_struct()
        cbf.read_file(filename, pycbf.MSG_DIGEST)
        cbf.select_datablock(0)
        cbf.select_category(0)
        cbf.select_column(2)
        imsize = cbf.get_image_size(0)
        return np.fromstring(cbf.get_integerarray_as_string(), 
                             np.int32).reshape(imsize)
    else:
        try:
            from nexpy.readers.tifffile import tifffile as TIFF
        except ImportError:
            raise NeXusError('Reading TIFF files requires the TIFF reader installed with NeXpy')
        if filename.endswith('.bz2'):
            import bz2
            tiff_file = TIFF.TiffFile(bz2.BZ2File(filename))
        else:
            tiff_file = TIFF.TiffFile(filename)
        return tiff_file.asarray()


def read_images(filenames, shape):
    good_files = [f for f in filenames if f is not None]
    if good_files:
        v0 = read_image(good_files[0])
        assert v0.shape == shape, 'Image shape of %s not consistent' % good_files[0]
        v = np.empty([len(filenames), v0.shape[0], v0.shape[1]], dtype=np.float32)
    else:
        v = np.empty([len(filenames), shape[0], shape[1]], dtype=np.float32)
    v.fill(np.nan)
    i = 0
    for filename in filenames:
        if filename:
            v[i] = read_image(filename)
        i += 1
    return v


def read_metadata(filename):
    if filename.endswith('bz2'):
        fname = os.path.splitext(filename)[0]
    else:
        fname = filename
    if os.path.splitext(fname)[1] == '.cbf':
        try:
            import pycbf
        except ImportError:
            raise NeXusError('Reading CBF files requires the pycbf module')
        cbf = pycbf.cbf_handle_struct()
        cbf.read_file(fname, pycbf.MSG_DIGEST)
        cbf.select_datablock(0)
        cbf.select_category(0)
        cbf.select_column(1)
        meta_text = cbf.get_value().splitlines()
        date_string = meta_text[2][2:]
        time_stamp = epoch(date_string)
        exposure = float(meta_text[5].split()[2])
        summed_exposures = 1
        return time_stamp, exposure, summed_exposures
    elif os.path.exists(fname+'.metadata'):
        parser = ConfigParser()
        parser.read(fname+'.metadata')
        return (parser.getfloat('metadata', 'timeStamp'),
                parser.getfloat('metadata', 'exposureTime'),
                parser.getint('metadata', 'summedExposures'))
    else:
        return time.time(), 1.0, 1


def read_specfile(spec_file):
    subprocess.call('spec2nexus --quiet '+spec_file, shell=True)
    subentries = []
    prefix = os.path.splitext(os.path.basename(spec_file))[0]
    directory = os.path.dirname(spec_file)
    try:
        spec = nxload(os.path.join(directory, prefix+'.hdf5'))
        for entry in spec.NXentry:
            entry.nxclass = NXsubentry
            subentries.append(entry)
    except:
        pass
    return subentries


def isotime(time_stamp):
    return datetime.fromtimestamp(time_stamp).isoformat()


def epoch(iso_time):
    d = datetime.strptime(iso_time,'%Y-%m-%dT%H:%M:%S.%f')
    return time.mktime(d.timetuple()) + (d.microsecond / 1e6)


def get_background(filename):
    scan_time, exposure, summed_exposures = read_metadata(filename)
    frame_time = summed_exposures * exposure
    data = read_image(filename).astype(np.float32)
    return data, frame_time


def initialize_nexus_file(directory, output_file, filenames, first):
    z_size = get_index(filenames[-1]) - get_index(filenames[0]) + 1
    v0 = read_image(filenames[0])
    x = NXfield(range(v0.shape[1]), dtype=np.uint16, name='x_pixel')
    y = NXfield(range(v0.shape[0]), dtype=np.uint16, name='y_pixel')
    if z_size > 1:
        z = first + np.arange(z_size)
        z = NXfield(z, dtype=np.uint16, name='frame_number', maxshape=(5000,))
        v = NXfield(name='data', shape=(z_size, v0.shape[0], v0.shape[1]),
                    dtype=np.float32, maxshape=(5000, v0.shape[0], v0.shape[1]))
        data = NXdata(v, (z,y,x))
    else:
        v = NXfield(name='data', shape=(v0.shape[0], v0.shape[1]), dtype=np.float32)
        data = NXdata(v, (y, x))
    root = NXroot(NXentry(data, NXsample(), NXinstrument(NXdetector())))
    root.entry.instrument.detector.frame_start = \
        NXfield(shape=(z_size,), maxshape=(5000,), units='s',
                dtype=np.float32)
    root.save(output_file, 'w')
    return root


def write_data(root, filenames, background_file=None):
    scan_time, exposure, summed_exposures = read_metadata(filenames[0])
    root.entry.start_time = isotime(scan_time)
    root.entry.instrument.detector.frame_time = summed_exposures * exposure
    if background_file:
        background_data, background_frame_time = get_background(background_file)
        frame_ratio = (background_frame_time /
                       root.entry.instrument.detector.frame_time)
        background = background_data / frame_ratio
        root.entry.instrument.detector.flatfield = background
        root.entry.instrument.detector.flatfield_applied = True
    else:
        background = 0.0
    if len(root.entry.data.data.shape) == 2:
        root.entry.data.data[:,:] = read_image(filenames[0])
    else:
        z_size = root.entry.data.data.shape[0]
        image_shape = root.entry.data.data.shape[1:3]
        chunk_size = root.nxfile['/entry/data/data'].chunks[0]
        min_index = get_index(filenames[0])
        max_index = get_index(filenames[-1])
        k = 0
        for i in range(min_index, min_index+z_size, chunk_size):
            try:
                files = []
                for j in range(i,i+chunk_size):
                    if j == get_index(filenames[k]):
                        print 'Processing', filenames[k]
                        files.append(filenames[k])
                        try:
                            exposure_time, exposure, summed_exposures = read_metadata(filenames[k])
                            root.entry.instrument.detector.frame_start[k] = exposure_time - scan_time
                        except Exception as error:
                            print filenames[k], error
                        k += 1
                    elif k < len(filenames):
                        files.append(None)
                    else:
                        break
                root.entry.data.data[i-min_index:i+chunk_size-min_index,:,:] = (
                    read_images(files, image_shape) - background)
            except IndexError:
                pass


def write_metadata(root, directory, prefix):
    if 'dark' in prefix:
        root.entry.sample.name = 'dark'
        root.entry.title = 'Dark Field'
    else:
        dirname=directory.split(os.path.sep)[-1]
        match = re.match('(.*?)_([0-9]+)k$', dirname)
        if match:
            try:
                sample = match.group(1)
                root.entry.sample.name = sample
                temperature = int(match.group(2))
                root.entry.sample.temperature = NXfield(temperature, units='K')
                root.entry.title = "%s %sK %s" % (sample, temperature, prefix)
            except Exception:
                pass
    root.entry.filename = root.nxfilename


def write_specfile(root, spec_file):
     subentries = read_specfile(spec_file)
     for subentry in subentries:
         root.entry[subentry.nxname] = subentry


def natural_sort(key):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', key)]    


def nxstack(directory, extension, output, version, 
            prefixes=None, 
            first=None, last=None, reverse=False, 
            compression=None, background=None, spec=None):
    if background:
        try:
            background_file = glob.glob(os.path.join(background+'*'+extension))[-1]
        except IndexError:
            if extension.endswith('bz2'):
                background_file = glob.glob(os.path.join(background+'*'+extension[:-4]))[-1]
            else:
                background_file = None
    else:
        background_file = None

    if prefixes is None:
        prefixes = get_prefixes(directory)
    if len(prefixes) > 1 and output is not None:
        raise NeXusError("Only one prefix allowed if the output file is specified")

    if compression:
        nxsetcompression(compression)

    if output is not None and os.path.splitext(output)[1] == '':
        output = output + '.nxs'    
    for prefix in prefixes:
        tic = timeit.default_timer()
        data_files = get_files(directory, prefix, extension, first, last, reverse)
        if output is None:
            output_file = prefix + '.nxs'
        else:
            output_file = output
        root = initialize_nexus_file(directory, output_file, data_files, first)
        write_data(root, data_files, background_file)
        write_metadata(root, directory, prefix)
        if spec_file:
            write_specfile(root, spec_file)
        note = NXnote('nxstack '+' '.join(sys.argv[1:]), 
                      ('Current machine: %s\n'
                       'Current working directory: %s\n'
                       'Data files: %s to %s\n'
                       'Background file: %s\n'
                       'SPEC file: %s')
                      % (socket.gethostname(), os.getcwd(), 
                         data_files[0], data_files[-1], 
                         background_file, spec_file))
        root.entry['nxstack'] = NXprocess(program='nxstack',
                                    sequence_index=len(root.entry.NXprocess)+1,
                                    version=__version__, note=note)
                                 
        toc = timeit.default_timer()
        print toc-tic, 'seconds for', output_file
