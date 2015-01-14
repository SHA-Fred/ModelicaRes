#!/usr/bin/python
# -*- coding: utf-8 -*-
r"""Classes and functions to read Dymola\ :sup:`®`-formatted binary (*.mat) and
text (*.txt) results

This format is also used by OpenModelica_ and by PyFMI_ via JModelica.org_.

Classes:

- :class:`Samples` - Specialized namedtuple to store the time and value
  information of a variable from Dymola\ :sup:`®`-formatted simulation results

Functions:

- :func:`read` - Read variables from a MATLAB\ :sup:`®` (*.mat) or text (*.txt)
  file with Dymola\ :sup:`®`-formatted results.

- :func:`readsim` - Load Dymola\ :sup:`®`-formatted simulation results.

- :func:`readlin` - Load Dymola\ :sup:`®`-formatted linearization results.

Errors are raised under the following conditions:

- **IOError**: The file cannot be accessed.

- **TypeError**: The file does not use the Dymola\ :sup:`®` format.

- **AssertionError**: The results are not of the expected type (simulation or
  linearization), the orientation of the data (normal or transposed) is unknown,
  or the format version is not supported.

- **KeyError**: An expected variable is missing.

- **IndexError**: A variable has the wrong shape.

The last three errors occur when the file uses the Dymola\ :sup:`®` format but
something else is wrong.


_OpenModelica: https://www.openmodelica.org/
_PyFMI: http://www.pyfmi.org/
_JModelica.org: http://www.jmodelica.org/
"""
__author__ = "Kevin Davies"
__email__ = "kdavies4@gmail.com"
__copyright__ = ("Copyright 2012-2014, Kevin Davies, Hawaii Natural Energy "
                 "Institute, and Georgia Tech Research Corporation")
__license__ = "BSD-compatible (see LICENSE.txt)"

# Standard pylint settings for this project:
# pylint: disable=I0011, C0302, C0325, R0903, R0904, R0912, R0913, R0914, R0915
# pylint: disable=I0011, W0141, W0142

# Other:
# pylint: disable=I0011, C0103, C0301

import numpy as np
import re

from collections import namedtuple
from control.matlab import ss
from itertools import count
from natu import units as U
from natu.exponents import Exponents
from natu.units import s as second
from scipy.io import loadmat
from scipy.io.matlab.mio_utils import chars_to_strings
from six import PY2

from ..simres import Variable
from ..util import next_nonblank


class Samples(namedtuple('Samples', ['times', 'signed_values', 'negated'])):

   """Specialized namedtuple to store the time and value information of a
   variable from Dymola\ :sup:`®`-formatted simulation results

   The negated field indicates if the values should be negated upon access.  By
   keeping the sign separate, the same savings that Dymola\ :sup:`®` achieves in
   file size is achieved in active memory.  It stems from the fact that many
   Modelica_ variables have opposite sign due to flow balances.


   .. _Modelica: http://www.modelica.org/
   """
   @property
   def values(self):
       """The values of the variable
       """
       return -self.signed_values if self.negated else self.signed_values


if PY2:
    # For most strings (those besides the description), Unicode isn't
    # necessary.  Unicode support is less integrated in Python 2; Unicode
    # strings are a special case that are represented by u'...' (which is
    # distracting in the examples).  Therefore, in Python 2 we'll only use
    # Unicode for the description strings.
    def get_strings(str_arr):
        """Return a list of strings from a character array.

        Strip the whitespace from the right and return it to the character set
        it was saved in.
        """
        return [line.rstrip(' \0').encode('latin-1')
                for line in chars_to_strings(str_arr)]
        # The encode part undoes scipy.io.loadmat's decoding.
else:
    # In Python 3, literal strings are Unicode by default
    # (http://stackoverflow.com/questions/6812031/how-to-make-unicode-string-with-python3),
    # and we need to leave the strings decoded because encoded strings are bytes
    # objects.
    def get_strings(str_arr):
        """Return a list of strings from a character array.

        Strip the whitespace from the right and recode it as utf-8.
        """
        return [line.rstrip(' \0').encode('latin-1').decode('utf-8')
                for line in chars_to_strings(str_arr)]
        # Modelica encodes using utf-8 but scipy.io.loadmat decodes using
        # latin-1, thus the encode ... decode part.


def loadtxt(file_name, variable_names=None, skip_header=1):
    r"""Read variables from a  Dymola\ :sup:`®`-formatted text file (*.txt).

    **Parameters:**

    - *file_name*: Name of the results file, including the path and extension

    - *variable_names*: List of the names of the variables to read

         Any variable with a name not in this list will be skipped, possibly
         saving some processing time.  If *variable_names* is *None*, then all
         variables will be read.

     - *skip_header*: Number of lines to skip at the beginning of the file

    **Returns:**

    1. A dictionary of variable names and values
    """

    SPLIT_DEFINITION = re.compile('(\w*) *(\w*) *\( *(\d*) *, *(\d*) *\)').match
    PARSERS = {'char': lambda get, rows:
                   [get().rstrip() for row in rows],
               'float': lambda get, rows:
                   np.array([np.fromstring(get().split('#')[0], float, sep=' ')
                             for row in rows]).T,
               'int': lambda get, rows:
                   np.array([np.fromstring(get().split('#')[0], int, sep=' ')
                             for row in rows])}

    with open(file_name) as f:

        # Skip the header.
        for i in range(skip_header):
            f.next()

        # Collect the variables and values.
        data = {}
        while True:

            # Read and parse the next variable definition.
            try:
                line = next_nonblank(f)
            except StopIteration:
                break # End of file
            type_string, name, nrows, ncols = SPLIT_DEFINITION(line).groups()

            # Parse the variable's value, if it is selected
            rows = range(int(nrows))
            if variable_names is None or name in variable_names:
                try:
                    parse = PARSERS[type_string]
                except KeyError:
                    raise KeyError('Unknown variable type: ' + type_string)
                try:
                    data[name] = parse(f.next, rows)
                except StopIteration:
                    raise ValueError('Unexpected end of file')
            else:
                # Skip the current variable.
                for row in rows:
                    f.next()
    return data


def read(fname, constants_only=False):
    r"""Read variables from a MATLAB\ :sup:`®` (*.mat) or text file (*.txt) with
    Dymola\ :sup:`®`-formatted results.

    **Parameters:**

    - *fname*: Name of the results file, including the path and extension

         This may be from a simulation or a linearization.

    - *constants_only*: *True* to assume the result is from a simulation and
      read only the variables from the first data matrix

    **Returns:**

    1. A dictionary of variable names and values

    2. A list of strings from the lines of the 'Aclass' matrix
    """

    # Load the file.
    variable_names = ['Aclass', 'name', 'names', 'description', 'dataInfo',
                      'data', 'data_1'] if constants_only else None
    try:
        data = loadmat(fname, variable_names=variable_names,
                       chars_as_strings=False, appendmat=False)
        binary = True
    except ValueError:
        data = loadtxt(fname, variable_names=variable_names)
        binary = False
    except IOError:
        raise IOError('"{}" could not be opened.  '
                      'Check that it exists.'.format(fname))

    # Get the Aclass variable and transpose the data if necessary.
    try:
        Aclass = data.pop('Aclass')
    except KeyError:
        raise TypeError('"{}" does not appear to use the Dymola format.  '
                        'The "Aclass" variable is missing.'.format(fname))
    if binary:
        Aclass = get_strings(Aclass)

        # Determine if the data is transposed.
        try:
            transposed = Aclass[3] == 'binTrans'
        except IndexError:
            transposed = False
        else:
            assert transposed or Aclass[3] == 'binNormal', (
                'The orientation of the Dymola-formatted results is not '
                'recognized.  The third line of the "Aclass" variable is "%s", '
                'but it should be "binNormal" or "binTrans".' % Aclass[3])

        # Undo the transposition and convert character arrays to strings.
        for name, value in data.items():
            if value.dtype == '<U1':
                data[name] = get_strings(value.T if transposed else value)
            elif transposed:
                data[name] = value.T

    else:
        # In a text file, only the data_1, data_2, etc. matrices are transposed.
        for name, value in data.items():
            if name.startswith('data_'):
                data[name] = value.T

    return data, Aclass

def readsim(fname, constants_only=False):
    r"""Load Dymola\ :sup:`®`-formatted simulation results.

    **Parameters:**

    - *fname*: Name of the results file, including the path and extension

    - *constants_only*: *True* to read only the variables from the first data
      matrix

         The first data matrix typically contains all of the constants,
         parameters, and variables that don't vary.  If only that information is
         needed, it may save resources to set *constants_only* to *True*.

    **Returns:** A dictionary of variables (instances of
    :class:`~modelicares.simres.Variable`)

    **Example:**

    >>> variables = readsim('examples/ChuaCircuit.mat')
    >>> variables['L.v'].unit
    'V'
    """
    # This does the task of mfiles/traj/tload.m from the Dymola installation.

    def parse_description(description):
        """Parse the a variable description string into unit, displayUnit, and
        description.

        Convert the unit into a :class:`natu.core.Unit`.  Convert the display 
        unit into an :class:`natu.exponents.Exponents` instance.  If the display 
        unit is not specified, use the unit instead. 
        """
        description = description.rstrip(']')
        displayUnit = ''
        try:
            description, unit = description.rsplit('[', 1)
        except ValueError:
            unit = ''
        else:
            unit = unit.replace('.', '*').replace('Ohm', 'ohm')
            try:
                unit, displayUnit = unit.rsplit('|', 1)
            except ValueError:
                pass  # (displayUnit = '')

        display_unit = displayUnit if displayUnit else unit
        unit = U._units(**Exponents(unit))
        description = description.rstrip()
        if PY2:
            description = description.decode('utf-8')

        return unit, display_unit, description

    # Load the file.
    data, Aclass = read(fname, constants_only)

    # Check the type of results.
    if Aclass[0] == 'AlinearSystem':
        raise AssertionError(fname + ' is a linearization result.  Use LinRes '
                             'instead.')
    assert Aclass[0] == 'Atrajectory', (fname + ' is not a simulation or '
                                        'linearization result.')

    # Process the name, description, parts of dataInfo, and data_i variables.
    # This section has been optimized for speed.  All time and value data
    # remains linked to the memory location where it is loaded by scipy.  The
    # negated variable is carried through so that copies aren't necessary.  If
    # changes are made to this code, be sure to compare the performance (e.g.,
    # using %timeit in IPython).
    version = Aclass[1]
    if version == '1.1':
        names = data['name']
        units_included = 'environment.baseUnits.c' in names

        # Extract the trajectories.
        trajectories = []
        for i in count(1):
            try: 
                trajectories.append(data['data_%i' % i])
            except KeyError:
                break
            if second._value <> 1.0:
                # Apply the value of the unit second.
                trajectories[-1][:, 0] *= second._value

        # Create the variables.
        variables = []
        for description, [data_set, sign_col] \
            in zip(data['description'], data['dataInfo'][:, 0:2]):
            unit, display_unit, description = parse_description(description)
            dimension = unit.dimension
            negated = sign_col < 0
            traj = trajectories[data_set - 1]
            signed_values =  traj[:, (-sign_col if negated else sign_col) - 1]                
            times = traj[:, 0]
            try:
                if unit._value <> 1.0:
                    signed_values *= unit._value
            except AttributeError:
                # Must be a LambdaUnit
                if negated:
                    signed_values = -signed_values
                    negated = False
                get_value = np.vectorize(lambda n: unit._toquantity(n)._value)
                signed_values = get_value(signed_values)
            variables.append(Variable(Samples(times, signed_values, negated), 
                                      dimension, display_unit, description))
        variables = dict(zip(names, variables))

        # Time is from the last data set.
        variables['Time'] = Variable(Samples(times, times, False),
                                     second.dimension, 's', 'Time')
        return variables

    elif version == '1.0':
        traj = data['data']
        times = traj[:, 0]*s._value
        return {name:
                Variable(Samples(times, traj[:, i], False), None, None, '')
                for i, name in enumerate(data['names'])}

    raise AssertionError("The version of the Dymola-formatted result file (%s) "
                         "isn't supported.")

       # TODO: assert these equal to natu:
       #            'environment.baseUnits.R_inf', 'environment.baseUnits.c',
       #            'environment.baseUnits.k_J', 'environment.baseUnits.R_K',
       #            'environment.baseUnits.k_F', 'environment.baseUnits.R',
       #            'environment.baseUnits.k_Aprime']))

"""TODO
Variable(Samples(times, signed_values, negated), dimension, display_unit,
         description)

All in one:
No dup of dimensions and units
If quantities disabled, then dimensions and units not tracked, but can still plot in various units
Quicker to retrieve
If quantities enabled and units unknown, then use floats for values, but can''t do unit conversion
display unit must be the same for all

Separate:
Quicker to load (prob only slightly)
If quantities disabled, then retrieved values have no dimensions or units, but can assume SI units and proper dimensions to do unit conversion

variable extends quantity? No
- Good: Variable is a Quantity
- Good: Both have dimension and display unit
- Good: _value property is the raw data with units included
- Good: values, FV, mean, etc. methods return quantities
- *Bad: variable has times, but quantity doesn't
- *Bad: quantity can be used as a mathematical entity, but variable can't
"""

def readlin(fname):
    r"""Load Dymola\ :sup:`®`-formatted linearization results.

    **Parameters:**

    - *fname*: Name of the results file, including the path and extension

    **Returns:**

    - An instance of :class:`control.StateSpace`, which contains:

         - *A*, *B*, *C*, *D*: Matrices of the linear system

              .. code-block:: modelica

                 der(x) = A*x + B*u;
                      y = C*x + D*u;

         - *state_names*: List of names of the states (*x*)

         - *input_names*: List of names of the inputs (*u*)

         - *output_names*: List of names of the outputs (*y*)

    **Example:**

    >>> sys = readlin('examples/PID.mat')
    >>> sys.state_names
    ['I.y', 'D.x']
    """
    # This does the task of mfiles/traj/tloadlin.m in the Dymola installation.

    # pylint: disable=I0011, W0621

    # Load the file.
    data, Aclass = read(fname)

    # Check the type of results.
    if Aclass[0] == 'Atrajectory':
        raise AssertionError(fname + ' is a simulation result.  Use SimRes '
                             'instead.')
    assert Aclass[0] == 'AlinearSystem', (fname + ' is not a simulation or'
                                          ' linearization result.')

    # Determine the number of states, inputs, and outputs.
    ABCD = data['ABCD']
    nx = data['nx'][0]
    nu = ABCD.shape[1] - nx
    ny = ABCD.shape[0] - nx

    # Extract the system matrices.
    A = ABCD[:nx, :nx] if nx > 0 else [[]]
    B = ABCD[:nx, nx:] if nx > 0 and nu > 0 else [[]]
    C = ABCD[nx:, :nx] if nx > 0 and ny > 0 else [[]]
    D = ABCD[nx:, nx:] if nu > 0 and ny > 0 else [[]]
    sys = ss(A, B, C, D)

    # Extract the variable names.
    xuyName = data['xuyName']
    sys.state_names = xuyName[:nx]
    sys.input_names = xuyName[nx:nx + nu]
    sys.output_names = xuyName[nx + nu:]

    return sys


if __name__ == '__main__':
    # Test the contents of this file.

    # pylint: disable=I0011, W0631

    import os
    import doctest

    if os.path.isdir('examples'):
        doctest.testmod()
    else:
        # Create a link to the examples folder.
        for example_dir in ['../examples', '../../examples']:
            if os.path.isdir(example_dir):
                break
        else:
            raise IOError("Could not find the examples folder.")
        try:
            os.symlink(example_dir, 'examples')
        except AttributeError:
            raise AttributeError("This method of testing isn't supported in "
                                 "Windows.  Use runtests.py in the base "
                                 "folder.")

        # Test the docstrings in this file.
        doctest.testmod()

        # Remove the link.
        os.remove('examples')
