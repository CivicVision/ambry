Ambry Databundles
================

Install
=======

See http://ambry.io for the general documentation, http://docs.ambry.io/en/latest/install_config/install.html for instgallation, 
and http://docs.ambry.io/en/latest/install_config/configuration.html for additional configuration. 

Setup with Miniconda on Mac
===========================

You can setup Ambry as a normal package, but the geographic library, GDAL, is really difficult to install, so your
Ambry installation won't produce geo databases. The best way to get GDAL installed is with Anaconda.

First, install miniconda, ( python 2.7 )

.. code-block:: bash

    $ wget http://repo.continuum.io/miniconda/Miniconda-latest-Linux-x86_64.sh -O miniconda.sh
    $ bash miniconda.sh -b

    # Activate the anaconda environment
    $ export PATH=~/miniconda/bin:$PATH

Now you can create the environment.

.. code-block:: bash

    $ conda create -n ambry python

    # Where did conda put it?
    $ conda info -e

    # Now, activate it.
    $ source activate ambry

More about creating conda virtual environments: http://conda.pydata.org/docs/faq.html#env-creating

After setting up ambry, you can use conda to install gdal

.. code-block:: bash

    $ git clone https://github.com/<githubid>/ambry.git
    $ cd ambry
    $ pip install -r requirements.txt
    $ conda install gdal
    $ python setup.py devel

Running the ambry tests
=======================
.. code-block:: bash

    $ git clone https://github.com/<githubid>/ambry.git
    $ cd ambry
    $ pip install -r requirements/dev.txt
    $ python setup.py test


Test command options:

.. code-block:: 

  --verbosity - verbosity of the tests, 1 by default.
  --failfast - if given, stop testing on first fail.

Examples:

.. code-block:: bash

    $ python setup.py test --verbosity=2 --failfast
    
Coverage 
--------

To test with coverage::

    $ coverage run setup.py test
    $ coverage html
    
Then, open htmlcov/index.html in a browser.

    


