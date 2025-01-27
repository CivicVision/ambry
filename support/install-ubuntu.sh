#!/bin/bash

is_dev=$1

echo "--- Installing Ambry"
apt-get update
locale-gen en_US.UTF-8

packages="git gcc g++ python-pip  python-dev sqlite3  libpq-dev libffi-dev
libgdal-dev gdal-bin python-gdal python-numpy python-scipy "

let "ver=$(lsb_release -r -s | tr -d '.')"

if (( $ver > 1404 )); then
    packages="$packages libsqlite3-dev libspatialite3 libspatialite-dev spatialite-bin libspatialindex-dev"
else
    packages="$packages libsqlite3-dev libspatialite5 libspatialite-dev spatialite-bin libspatialindex-dev"
fi

apt-get install -y $packages
if [ $? != 0 ]; then
    echo "ERROR: Failed to install $pkg"
    exit $?
fi

pip install -U pip


# This package allows Sqlalchemy to load the spatialite shared object to provide
# Spatialite services.
pip install git+https://github.com/clarinova/pysqlite.git#egg=pysqlite

###
### Install Ambry
###

if [ ! -z "$is_dev" ]; then
    pip install -e "git+https://github.com/CivicKnowledge/ambry.git@develop#egg=ambry"
else
    pip install "git+https://github.com/CivicKnowledge/ambry.git"
fi

ambry config install # Installs a development config
