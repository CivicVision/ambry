"""A Library is a local collection of bundles. It holds a database for the configuration
of the bundles that have been installed into it.
"""

# Copyright (c) 2013 Clarinova. This file is licensed under the terms of the
# Revised BSD License, included in this distribution as LICENSE.txt

from collections import defaultdict
import logging
import re
import os.path
from StringIO import StringIO
import time

import unicodecsv as csv

from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.exc import OperationalError, IntegrityError

from sqlite3 import DatabaseError as SqliteDatabaseError

from boto.exception import S3ResponseError

from ckcache.multi import AltReadCache, MultiCache

from ambry.dbexceptions import ConfigurationError, NotFoundError, DependencyError, DatabaseError,\
    ConflictError, NotABundle

from ambry.orm import Dataset, Table, Column, Partition

from ..identity import LocationRef, Identity, NotObjectNumberError
from ..util import memoize, Progressor
from ..bundle import LibraryDbBundle

import weakref

from files import Files


libraries = {}


def _new_library(config):
    from database import LibraryDb
    from ckcache import new_cache

    cache = new_cache(config['filesystem'])

    database = LibraryDb(**dict(config['database']))

    try:
        database.create()
    except OperationalError as e:

        raise DatabaseError('Failed to create {} : {}'.format(database.dsn, e.message))

    if 'upstream' in config:
        raise DeprecationWarning("Upstream no longer allowed in configuration")

    remotes = {}

    for name, remote in config.get('remotes', {}).items():

        try:
            remotes[name] = new_cache(remote)
        except S3ResponseError as e:
            from ..util import get_logger

            logger = get_logger(__name__)
            logger.error('Failed to init cache {} : {}; {} '.format(name, str(remote.bucket), e))

    for i, remote in enumerate(remotes.values()):
        remote.set_priority(i)

    source_dir = config.get('source', None)

    if 'documentation' in config:
        doc_cache = new_cache(config['documentation'])
    else:
        doc_cache = cache.subcache('_doc')

    if 'warehouses' in config:
        warehouse_cache = new_cache(config['warehouses'])
    else:
        warehouse_cache = cache.subcache('warehouses')

    l = Library(cache=cache,
                doc_cache=doc_cache,
                warehouse_cache=warehouse_cache,
                database=database,
                name=config['_name'] if '_name' in config else 'NONE',
                remotes=remotes,
                source_dir=source_dir)

    return l


def new_library(config=None, reset=False):
    """Return a new :class:`~ambry.library.Library`, constructed from a
    configuration

    :param config: a :class:`~ambry.run.RunConfig` object
    :rtype:  :class:`~ambry.library.Library`

    If ``config`` is None, the function will constuct a new
    :class:`~ambry.run.RunConfig` with a default constructor.

    """

    global libraries

    if config is None:
        from ..run import get_runconfig
        config = get_runconfig().library('default')

    if reset:
        libraries = {}

    name = config.get('_name', None)

    if name is None:
        name = 'default'

    if name not in libraries:
        libraries[name] = _new_library(config)

    l = libraries[name]
    l.clear_dependencies()

    return l


def clear_libraries():
    global libraries

    libraries = {}


class Library(object):

    def __init__(self, cache, database,
                 name=None, remotes=None, source_dir=None,
                 doc_cache=None, warehouse_cache=None):

        '''Libraries are constructed on the root cache name for the library.
        If the cache does not exist, it will be created.

        Args:

        cache: a path name to a directory where bundle files will be stored
        database:
        remote: URL of a remote library, for fallback for get and put.
        sync: If true, put to remote synchronously. Defaults to False.

        '''
        from ..util import get_logger

        assert database is not None

        self.name = name
        self.cache = cache
        self._doc_cache = doc_cache
        self._warehouse_cache = warehouse_cache
        self.source_dir = source_dir

        self._database = database
        self._bundle = None  # Set externally in bundle.library()

        self.dep_cb = None  # Callback for dependency resolution
        self._dependencies = None
        self._remotes = remotes

        self._all_vids = None

        if not self.cache:
            raise ConfigurationError("Must specify library.cache for the "
                                     "library in bundles.yaml")

        self.logger = get_logger(__name__)
        self.logger.setLevel(logging.DEBUG)

        self.needs_update = False

        self.bundles = weakref.WeakValueDictionary()

        self._search = None

    def clone(self):

        return self.__class__(self.cache, self.database.clone())

    def _create_bundle(self, path):
        from ..bundle.bundle import DbBundle

        if path in self.bundles:
            return self.bundles[path]

        bundle = DbBundle(path)

        self.bundles[path] = bundle

        return bundle

    def close(self):

        for path, bundle in self.bundles.items():
            bundle.close()

        self.database.close()

    @property
    def database(self):
        '''Return ambry.database.Database object'''
        return self._database

    def commit(self):
        self.database.commit()

    #
    # Configurables
    #

    def _meta_set(self, key, value):

        return self.database.set_config_value('library', key, value)

    def _meta_get(self, key):

        try:
            return self.database.get_config_value('library', key).value
        except AttributeError:
            return None


    #
    # Storing
    #

    def put_bundle(self, bundle, source=None, install_partitions=True,
                   file_state='installed', commit=True, force=False):
        """Install the records for the dataset, tables, columns and possibly
        partitions. Does not install file references """

        if not force and self.files.query.ref(bundle.identity.vid).type(Files.TYPE.BUNDLE).one_maybe:
            return self.cache.path(bundle.identity.cache_key), False

        self.database.install_bundle(bundle)

        if source is None:
            source = self.cache.repo_id

        installed = self.files.install_bundle_file(bundle, source, commit=commit, state=file_state)

        if install_partitions:
            for p in bundle.partitions.all:
                self.put_partition(p, source, commit=commit, file_state=file_state)

        # Copy the file in if we don't have it already
        if not self.cache.has(bundle.identity.cache_key):
            self.cache.put(bundle.database.path, bundle.identity.cache_key)

        if self._doc_cache:
            self.search.index_dataset(bundle, force=True)

            for partition in bundle.partitions:
                self.search.index_partition(partition, force=True)

            self.search.commit()

        self.mark_updated(vid=bundle.identity.vid)
        self.mark_updated(key='bundle_index')
        self.mark_updated(key='library_info')

        return self.cache.path(bundle.identity.cache_key), installed

    def put_partition(self, partition, source=None, file_state='installed', commit=True):
        """Install the record and file reference for the partition """

        if source is None:
            source = self.cache.repo_id

        installed = self.files.install_partition_file(partition, source, commit=commit, state=file_state)

        # Ref partitions use the file of an earlier version, so there is no file to install
        if not self.cache.has(partition.identity.cache_key) and os.path.exists(partition.database.path):
            self.cache.put(partition.database.path, partition.identity.cache_key)

        return self.cache.path(partition.identity.cache_key), installed

    def remove(self, bundle):
        '''Remove a bundle from the library, and delete the configuration for
        it from the library database'''

        self.database.remove_bundle(bundle)

        self.mark_updated(vid=bundle.identity.vid)

        self.cache.remove(bundle.identity.cache_key, propagate=True)

    #
    # Retreiving
    #

    def list(self, datasets=None, with_partitions=False):
        '''Lists all of the datasets in the partition, optionally with
        metadata. Does not include partitions. This returns a dictionary
        in  a form that is similar to the remote and source lists. '''

        if datasets is None:
            datasets = {}

        self.database.list(datasets=datasets, with_partitions=with_partitions)

        return datasets

    def list_bundles(self, last_version_only=True,
                     locations=[LocationRef.LOCATION.LIBRARY], key=None):
        """Like list(), but returns bundles instead of a dict with identities.
        key is a parameter to sorted(self.list())"""

        if last_version_only:

            # Unlike other properties, name is an object, not a string, so
            # it won't compare properly unless you cast it.
            if key is None:
                key = lambda ident: str(ident.name)

            def rev_cmp(a, b):

                ka = key(a)
                kb = key(b)

                if ka == kb:
                    return - cmp(a.on.revision, b.on.revision)
                else:
                    return cmp(ka, kb)

            current = None

            for ident in sorted(self.list().values(), cmp=rev_cmp):

                if locations and not ident.locations.has(locations):
                    continue

                if not current or ident.id_ != current.identity.id_:

                    if current:
                        yield current
                        current.close()

                    try:

                        current = LibraryDbBundle(self.database, ident.vid)

                        current.identity.data['other_versions'] = set()
                    except NotFoundError:

                        # This happens frequently in warehosues, where only one version of the
                        # dataset is installed.
                        pass
                else:

                    current.identity.data['other_versions'].add(ident)

            if current:
                yield current
                current.close()

        else:

            if key is None:
                key = lambda ident: ident.vname

            for ident in sorted(self.list().values(), key=key):
                b = LibraryDbBundle(self.database, ident.vid)
                yield b
                b.close()

    def path(self, rel_path):
        """Return the cache path for a cache key"""

        return self.cache.path(rel_path)

    @property
    def remote_stack(self):
        """Return a MultiCache that has all of the remotes as upstreams"""
        return MultiCache(self.remotes.values())

    def _get_bundle_by_cache_key(self, cache_key, cb=None):
        """Load a bundle from either the local cache, or the remote if it is not local """

        # The AltReadCache will get from the remote stack if the path is not found in the cache.
        arc = AltReadCache(self.cache, self.remote_stack)
        abs_path = arc.get(cache_key, cb=cb)

        if not abs_path or not os.path.exists(abs_path):
            return False

        try:
            bundle = self._create_bundle(abs_path)
        except SqliteDatabaseError as e:
            self.logger.error("Failed to load databundle at path {}: {}".format(abs_path, e))
            raise
        except AttributeError as e:
            self.logger.error("Failed to load databundle at path {}: {}".format(abs_path, e))
            raise  # DatabaseError
        except OperationalError as e:
            self.logger.error("Failed to load databundle at path {}: {}".format(abs_path, e))
            raise SqliteDatabaseError

        bundle.library = self

        return bundle

    def has(self, ref, location='default'):
        dataset = self.resolve(ref, location=location)

        if dataset.partition:
            return self.cache.has(dataset.partition.cache_key)
        else:
            return self.cache.has(dataset.cache_key)

    def get(self, ref, remote=None, force=False, cb=None, location='default'):
        '''Get a bundle, given an id string or a name '''
        from ckcache import copy_file_or_flo

        # Get a reference to the dataset, partition and relative path
        # from the local database.

        dataset = self.resolve(ref, location=location)

        if not dataset:
            msg = "Failed to resolve reference '{}' in library '{}' ".format(
                ref, self.database.dsn)
            raise NotFoundError(msg)

        dataset_files = self.files.query.type(Files.TYPE.BUNDLE).ref(dataset.vid).all

        if dataset_files:

            df_remotes = [df.source_url for df in dataset_files]

            # Get the remote that the bundle came from. TODO, this just finds the first one,
            # somewhat randomly.
            # There should be a priority to which remote it finds. Maybe the file File.all method
            # should sort by priority

            if remote is None:
                for r in self.remotes.values():
                    if r.repo_id in df_remotes:
                        remote = r
                        break
        else:
            remote = None

        # BUndle head is always installed, no need for remote
        bundle = self._get_bundle_by_cache_key(dataset.cache_key)

        if not bundle:
            raise NotFoundError(
                "Failed to get bundle from cache key: '{}'".format(dataset.cache_key))

        try:
            if dataset.partition:

                partition = bundle.partitions.get(dataset.partition.vid)

                if not partition:
                    raise NotFoundError('Failed to get partition {} from bundle at {} '
                                        .format(dataset.partition.fqname, dataset.cache_key))

                if not self.cache.has(partition.identity.cache_key):

                    if not remote:
                        msg = "No remote defined for nonlocal partition. Expected "\
                            "remote name '{}' "\
                            .format(df.source_url)
                        raise NotFoundError(msg)

                    # If the partition has a reference, get that instead. This will
                    # load it into the local file
                    if partition.ref:

                        source_ident = self.resolve(partition.ref)

                        # TODO! requiring the prior version to exist in the library will be a problem,
                        # since the current sync_remote will avoid loading old version of a bundle.
                        if not source_ident:
                            msg = "Reference '{}' refers to another partition, '{}',"\
                                "which does not exist"\
                                .format(ref, partition.ref)
                            raise NotFoundError(msg)
                    else:
                        source_ident = partition.identity

                    # Copy the partition from the remote to the local cache.
                    with self.cache.put_stream(partition.identity.cache_key) as out_s:
                        in_s = remote.get_stream(source_ident.cache_key, cb=cb)

                        if not in_s:
                            raise NotFoundError('Failed to get partition {} from cache {}'
                                                .format(partition.identity.cache_key, str(remote)))
                        copy_file_or_flo(in_s, out_s)

                    abs_path = self.cache.path(partition.identity.cache_key)

                    if not abs_path or not os.path.exists(abs_path):
                        raise NotFoundError('Failed to get partition {} from cache {}'
                                            .format(partition.identity.cache_key, str(remote)))

                    source = remote.repo_id

                else:

                    source = self.cache.repo_id

                self.put_partition(partition, source, 'installed')

                # Attach the partition into the bundle, and return both.
                bundle.partition = partition

        finally:
            if bundle:
                bundle.close()

        return bundle

    @property
    def tables(self):
        """Return ORM records for all tables"""
        return self.database.session.query(Table).all()

    @property
    def tables_no_columns(self):
        """Return ORM records for all tables"""
        from sqlalchemy.orm import lazyload
        return self.database.session.query(Table).options(lazyload('columns')).all()

    def table(self, vid):
        try:
            return (self.database.session.query(Table).filter(Table.vid == vid).one())
        except NoResultFound:
            # Ths vid is actually an id, so we take the latest one
            try:
                t = self.database.session.query(Table)\
                    .filter(Table.id_ == vid)\
                    .order_by(Table.vid.desc())\
                    .first()

                if not t:
                    raise NoResultFound

                return t

            except NoResultFound:
                raise NotFoundError(
                    "Did not find table ref {} in library {}".format(vid, self.database.dsn))

    def column(self, vid):
        try:
            return self.database.session.query(Column).filter(Column.vid == vid).one()
        except NoResultFound:
            # Ths vid is actually an id, so we take the latest one
            try:
                t = self.database.session.query(Column)\
                    .filter(Column.id_ == vid)\
                    .order_by(Column.vid.desc())\
                    .first()

                if not t:
                    raise NoResultFound

                return t

            except NoResultFound:
                raise NotFoundError(
                    'Did not find Column ref {} in library {}'.format(vid, self.database.dsn))

    def derived_tables(self, proto_vid):
        """Tables with the given proto_vid"""
        return self.database.session.query(Table).filter(Table.proto_vid == proto_vid).all()

    def dataset(self, vid):
        try:
            return self.database.session.query(Dataset).filter(Dataset.vid == vid).one()
        except NoResultFound:
            try:
                ds = self.database.session.query(Dataset)\
                    .filter(Dataset.id_ == vid)\
                    .order_by(Dataset.revision.desc())\
                    .first()

                if ds is None:
                    raise NoResultFound
                else:
                    return ds

            except NoResultFound:
                raise NotFoundError("Failed to find dataset for ref '{}' ".format(vid))

    def datasets(self):
        return (self.database.session.query(Dataset).all())

    def versioned_datasets(self):
        """Like datasets(), but returns a dict structure, and only the most recent
        version, with other versions under the 'other_version' key """
        datasets = {}

        for ds in (self.database.session.query(Dataset).order_by(Dataset.revision.desc()).all()):

            if ds.id_ not in datasets:
                datasets[ds.id_] = ds.dict
                datasets[ds.id_]['other_versions'] = {}

            else:
                datasets[ds.id_]['other_versions'][ds.vid] = ds.dict

        return datasets

    def bundle(self, vid):
        """Returns a LibraryDbBundle for the given vid"""
        b = LibraryDbBundle(self.database, vid)
        b._library = self
        return b

    def partition(self, vid):
        # shortcut for parititon query
        query = self.database.session.query(Partition)
        try:
            return query.filter(Partition.vid == vid).one()
        except NoResultFound:
            try:
                return query.filter(Partition.id_ == vid).one()
            except NoResultFound:
                self.logger.error('No partition found: {} for {}'.format(vid, self.database.dsn))
                raise NotFoundError('No partition in library for vid : {} '.format(vid))

    def dataset_partitions(self, vid):
        """Return all partition records for a dataset vid"""
        return self.database.session.query(Partition).filter(Partition.d_vid == vid).all()

    @property
    def partitions(self):
        return self.database.session.query(Partition).all()

    @property
    def stores(self):
        """Return all of the refistered data stores. """
        return self.files.query.type(self.files.TYPE.STORE).all

    def store(self, uid):
        """Return a tuple of a manifest file object and the manifest. . """

        f = self.files.query.type(self.files.TYPE.STORE).ref(uid).one_maybe

        if not f:
            f = self.files.query.type(self.files.TYPE.STORE).path(uid).one_maybe

        return f

    def remove_store(self, uid):

        try:
            w = self.warehouse(uid)
            w.delete()
        except NotFoundError:
            self.logger.error("Didn't find warehouse for uid: {}".format(uid))

        s = self.store(uid)

        if s:
            self.mark_updated(vid=s.ref)
            self.database.session.delete(s)
            self.database.commit()

    def warehouse(self, uid):

        from ambry.warehouse import new_warehouse, database_config

        s = self.store(uid)

        if not s:
            raise NotFoundError("Did not find warehouse for uid: '{}' ".format(uid))

        config = database_config(s.path)

        return new_warehouse(config, self, logger=self.logger)

    @property
    def manifests(self):
        """Return all of the registered manifests. """

        # Construct manifest with Manifest(f.content, logger=self.logger)

        return self.files.query.type(self.files.TYPE.MANIFEST).all

    def manifest(self, uid):
        """Return a tuple of a manifest file object and the manifest. . """

        from ..warehouse.manifest import Manifest

        f = self.files.query.type(self.files.TYPE.MANIFEST).ref(uid).one_maybe

        if not f:
            return None, None

        return f, Manifest(f.content)

    def remove_manifest(self, uid):

        f, m = self.manifest(uid)

        if not f:
            raise NotFoundError("Didn't find manifest for uid '{}' ".format(uid))

        self.mark_updated(vid=f.ref)

        self.database.session.delete(f)
        self.database.commit()

    @property
    def remotes(self):

        if not self._remotes:
            return {}

        return self._remotes

    #
    # Finding
    #

    @property
    def resolver(self):
        return self.database.resolver

    def resolve(self, ref, location='default'):

        # If the location is not explicitly defined, set it to everything but source
        if location is 'default':
            location = [LocationRef.LOCATION.LIBRARY, LocationRef.LOCATION.PARTITION,
                        LocationRef.LOCATION.REMOTE]

        if isinstance(ref, Identity):
            ref = ref.vid

        resolver = self.resolver

        try:
            ip, ident = resolver.resolve_ref_one(ref, location)
        except NotObjectNumberError:
            ip, ident = None, None

        try:
            if ident and self.source:
                ident.bundle_path = self.source.source_path(ident=ident)
        except ConfigurationError:
            pass  # Warehouse libraries don't have source directories.

        return ident

    def locate(self, ref):
        """Return list of files for a reference, indicating where a file for a partition
            or dataset is located"""

        if isinstance(ref, Identity):
            ident = ref
        else:
            ident = self.resolve(ref)

        if not ident:
            return None, None

        if self.cache.has(ident.cache_key):
            return ident, self.cache

        for remote in sorted(self.remotes.values(), key=lambda x: x.priority):
            if remote.has(ident.cache_key):
                return ident, remote

        return ident, None

    #
    # Dependencies
    #

    def dep(self, name):
        """"Bundle version of get(), which uses a key in the
        bundles configuration group 'dependencies' to resolve to a name"""

        object_ref = self.dependencies.get(name, False)

        if not object_ref:
            raise DependencyError("No dependency named '{}'".format(name))

        try:
            o = self.get(object_ref)
        except NoResultFound:
            o = None

        if not o:
            raise DependencyError("Failed to get dependency, key={}, id={}".format(name, object_ref))

        if self.dep_cb:
            self.dep_cb(self, name, object_ref, o)

        return o

    @property
    def dependencies(self):

        if not self._dependencies:
            self._dependencies = self._get_dependencies()

        return self._dependencies

    def clear_dependencies(self):
        self._dependencies = None

    def _get_dependencies(self):

        if not self._bundle:
            raise ConfigurationError(
                "Can't use the dep() method for a library that is not attached to a bundle")

        deps = self._bundle.metadata.dependencies

        if not deps:
            return {}

        out = {}
        for k, v in deps.items():

            ident = self.resolve(v)
            if not ident:
                raise DependencyError("Failed to resolve {} ".format(v))

            if ident.partition:
                out[k] = ident.partition
            else:
                out[k] = ident

        return out

    def check_dependencies(self, throw=True, download=True):

        errors = {}
        for k, v in self.dependencies.items():

            if download:
                self.logger.info('Download and check dependency: {}'.format(v))

                b = self.get(v, cb=Progressor().progress)

                if not b:
                    if throw:
                        raise NotFoundError('Dependency check failed for '
                                            'key={}, id={}. Failed to get '
                                            'bundle or partition'.format(k, v))
                    else:
                        errors[k] = v

            else:
                self.logger.info('Check dependency: {}'.format(v))

                dataset = self.resolve(v)

                if not dataset:
                    if throw:
                        raise NotFoundError('Dependency check failed for '
                                            'key={}, id={}. Failed to get bundle or '
                                            'partition'.format(k, v))
                    else:
                        errors[k] = v

        return errors

    @property
    @memoize
    def files(self):
        return Files(self.database)

    @property
    def source(self):
        '''Return a SourceTree object, based on the source_repo configuration'''
        from ..source import SourceTree

        if not self.source_dir:
            return None

        return SourceTree(self.source_dir, self, self.logger)

    @property
    def new_files(self):
        '''Generator that returns files that should be pushed to the remote
        library'''

        new_files = self.files.query.installed.state('new').all

        for nf in new_files:
            yield nf

    def push(self, upstream, ref=None, cb=None, dry_run=False):
        """Push any files marked 'new' to the upstream

        Args:
            file_: If set, push a single file, obtailed from new_files.
            If not, push all files.

        """

        what = None
        start = None
        end = None

        if not upstream:
            raise Exception("Can't push() without defining a upstream. ")

        if ref is not None:

            ip, dsid = self.resolver.resolve_ref_one(ref)

            if not dsid:
                raise Exception("Didn't get id from database for ref: {}"
                                .format(ref))

            if dsid.partition:
                identity = dsid.partition
            else:
                identity = dsid

            try:
                file_ = self.files.query.installed.ref(identity.vid).state('new').one
            except Exception as exc:
                self.logger.error('Failed for {} with \'{}\' error'.format(identity.vid, exc))
                raise

            md = identity.to_meta(file=file_.path)

            if upstream.has(identity.cache_key):
                if cb:
                    cb('Has', md, 0)
                what = 'has'

                if not dry_run:
                    file_.state = 'pushed'

            else:
                start = time.clock()
                if cb:
                    cb('Pushing to {}'.format(upstream), md, start)

                if not dry_run:
                    upstream.put(file_.path, identity.cache_key, metadata=md)
                    file_.state = 'pushed'

                end = time.clock()
                dt = end - start

                if cb:
                    cb('Pushed', md, dt)
                what = 'pushed'

            if not dry_run:
                self.database.session.merge(file_)
                self.database.commit()

            return what, start, end, md['size'] if 'size' in md else None

        else:

            for ref in [file_.ref for file_ in self.new_files]:
                self.push(upstream, ref, cb=cb, dry_run=dry_run)

            try:
                upstream.store_list()
            except AttributeError:
                pass

    #
    # Maintainence
    #

    def clean(self, add_config_root=True):
        self.database.clean(add_config_root=add_config_root)

    def purge(self):
        """Remove all records from the library database, then delete all
        files from the cache"""
        self.clean()
        self.cache.clean()

    #
    # Synchronize
    #

    def mark_updated(self, o=None, vid=None, key=None):
        """Mark an object as recently updated, for instance to clear
        the doc_cache"""

        self.doc_cache.remove(vid, _key=key)

    def sync_library(self, clean=False):
        '''Rebuild the database from the bundles that are already installed
        in the repository cache'''

        assert Files.TYPE.BUNDLE == Dataset.LOCATION.LIBRARY
        assert Files.TYPE.PARTITION == Dataset.LOCATION.PARTITION

        if clean:
            self.files.query.type(Files.TYPE.BUNDLE).delete()
            self.files.query.type(Files.TYPE.PARTITION).delete()

        bundles = []

        self.logger.info('Rebuilding from dir {}'.format(self.cache.cache_dir))

        for r, d, f in os.walk(self.cache.cache_dir, topdown=True):

            # Exclude all of the directories which have the same basename
            # as a database file. These hold only partitions.
            d[:] = [dr for dr in d if dr + ".db" not in f]

            if '/meta/' in r:
                continue

            for file_ in f:

                if file_.endswith(".db"):

                    path_ = os.path.join(r, file_)

                    b = None
                    try:
                        b = self._create_bundle(path_)

                        try:
                            b.identity
                        except Exception as e:
                            self.logger.error("Failed to open bundle from {}: {} ".format(path_, e))
                            continue

                        if b.identity.is_bundle:
                            bundles.append(b)

                    except NotFoundError:
                        # Probably a partition, not a bundle.
                        pass
                    except Exception as e:
                        self.logger.error('Failed to process {}, {} : {} '
                                          .format(file_, path_, e))
                        raise
                    finally:
                        if b:
                            b.close()

        bundles = sorted(bundles, key=lambda b: b.partitions.count)

        for bundle in bundles:

            self.logger.info('Installing: {} '.format(bundle.identity.vname))

            try:
                try:
                    # Not installing partitions so can report progress on them later
                    self.put_bundle(bundle, self.cache.repo_id, install_partitions=False, commit=True)
                    installed = True
                except ConflictError:
                    installed = False

                try:

                    if installed:
                        for p in bundle.partitions:
                            if self.put_partition(p, self.cache.repo_id, commit=False):
                                self.logger.info('            {} '.format(p.identity.vname))

                    self.database.commit()
                    self.database.close()
                except Exception as e:
                    self.logger.error('Failed to sync {}; {}'.format(bundle.identity.vname, e))
                    raise

                bundle.close()

            except Exception as e:
                self.logger.error('Failed to install bundle {}: {}'
                                  .format(bundle.identity.vname, e.message))
                raise

        return bundles

    def sync_remotes(self, remotes=None, clean=False, last_only=True, vids=None):
        """Sync the local library with all of the remotes,
        create static JSON, and  build the full-text search index"""

        if clean:
            for remote_name, remote in remotes.items():
                self.files.query\
                    .type((Files.TYPE.PARTITION, Files.TYPE.BUNDLE))\
                    .source_url(remote.repo_id)\
                    .delete()

        if not remotes:
            remotes = self.remotes

        if not remotes:
            return

        for remote_name, remote in remotes.items():

            try:
                remote_list = remote.list().keys()
                self.logger.info("Syncing from remote: {} -> {} ".format(remote_name, remote))
            except S3ResponseError as e:
                self.logger.error("Failed to get list from {} -> {} : {} ".format(remote_name, remote, e))
                continue

            remote_files = self.files.query.type(Dataset.LOCATION.REMOTE).source_url(remote.repo_id).all
            all_keys = [f.path for f in remote_files]

            last_keys = defaultdict(lambda: [0, ''])

            use_only = None

            if last_only:
                use_only = []
                for cache_key in remote_list:
                    # Key without the version
                    nv_key = re.sub(r'-\d+\.\d+\.\d+\.db', '', cache_key)
                    try:
                        version = int(re.search(r'(\d+)\.db$', cache_key).group(1))
                    except AttributeError:
                        self.logger.error("Failed to find version numbers in '{}' ".format(cache_key))

                    if version > last_keys[nv_key][0]:
                        last_keys[nv_key] = [version, cache_key]

                for version, cache_key in last_keys.values():
                    use_only.append(cache_key)

            for cache_key in remote_list:

                if cache_key in all_keys:
                    continue

                if use_only and cache_key not in use_only:
                    self.logger.info("Skip old version: ({}".format(cache_key))
                    continue

                if self.cache.has(cache_key):  # This is just for reporting.
                    self.logger.info("Remote {} has: {}".format(remote.repo_id, cache_key))

                else:
                    self.logger.info("Remote {} sync: {}".format(remote.repo_id, cache_key))

                try:
                    b = self._get_bundle_by_cache_key(cache_key)
                except S3ResponseError as e:
                    self.logger.error("Failed to get {} from {} : {} ".format(cache_key, remote, e))
                    continue

                if not b:
                    self.logger.error("Failed to fetch bundle for {} ".format(cache_key))
                    continue

                vid = str(b.identity.vid)

                if vids and vid not in vids:
                    b.close()
                    continue

                try:
                    self.put_bundle(b, remote.repo_id, install_partitions=False,
                                    commit=True, file_state='installed')

                except NotABundle:
                    self.logger.error("Cache key {} exists, but isn't a valid bundle".format(cache_key))
                    b.close()
                    continue
                except Exception as e:
                    self.logger.error("Failed to put bundle {}: {}".format(cache_key, e))
                    b.close()
                    raise

                self.database.commit()
                self.database.close()
                b.close()

        self.mark_updated(key="bundle_index")
        self.mark_updated(key="library_info")

    def sync_source(self, clean=False):
        """Rebuild the database from the bundles that are already installed
        in the repository cache"""
        if clean:
            self.files.query.type(Dataset.LOCATION.SOURCE).delete()

        for ident in self.source._dir_list().values():

            try:
                path = ident.bundle_path
                self.sync_source_dir(ident, path)

            except Exception as e:
                self.logger.error("Failed to sync: bundle_path={} : {} ".format(ident.bundle_path, e.message))

        self.database.commit()

    def sync_source_dir(self, ident, path):

        self.logger.info('Installing: {} '.format(ident.vname))
        try:
            self.database.install_dataset_identity(ident)
            self.database.commit()

        except (ConflictError, IntegrityError) as e:
            self.logger.error(e)
            self.database.rollback()

        try:
            bundle = self.source.bundle(path, buildbundle_ok=True)

            self.files.install_bundle_source(bundle, self.source, commit=True)
            bundle.close()
            self.database.commit()

        except IntegrityError as e:
            self.logger.error(e)
            self.database.rollback()

    def sync_warehouse(self, w):
        """Create a reference to the warehouse and link all
        of the partitions to it. """


        store = self.files.install_data_store(w, name=w.name, title=w.title,  summary=w.summary)

        s = self.database.session

        # First, load in the partitions.

        for remote_p in w.library.partitions:
            p = self.partition(remote_p.vid)

            store.link_partition(p)
            p.link_store(store)
            p.dataset.link_store(store)

        # Next, we can load the manifests.

        for remote_manifest in w.manifests:

            # Copy the file record. There really should be an easier way
            # to do this.

            local_manifest = self.files.install_manifest(remote_manifest, warehouse=w)

            for p in remote_manifest.linked_partitions:
                p = self.partition(p.vid)

                local_manifest.link_partition(p)
                p.link_manifest(local_manifest)

                p.dataset.link_manifest(local_manifest)

            local_manifest.link_store(store)

            # This is the cheaper way to copy links, but it only works
            # when the linksare one-directional.
            local_manifest.data['tables'] = remote_manifest.data.get('tables', [])

            local_manifest.link_store(store)
            store.link_manifest(local_manifest)

        s.commit()

        self.mark_updated(vid=store.ref)

        return store

    def sync_warehouses(self):

        for f in self.stores:
            w = self.warehouse(f.path)
            self.logger.info('Syncing {} dsn={}'.format(f.ref, f.path))
            self.sync_warehouse(w)

    @property
    def doc_cache(self):
        """Return the documentation cache. """
        from ambry.library.doccache import DocCache
        return DocCache(self)

    @property
    def warehouse_cache(self):
        """Cache for warehouse Sqlite databases and extracts"""

        return self._warehouse_cache

    @property
    def search(self):
        from search import Search

        if not self._search:
            self._search = Search(self)

        return self._search

    def _gen_schema(self):
        from ..schema import Schema

        return Schema._dump_gen(self)

    def schema_as_csv(self, f=None):

        if f is None:
            f = StringIO()

        g = self._gen_schema()

        header = g.next()

        w = csv.DictWriter(f, header, encoding='utf-8')
        w.writeheader()
        last_table = None
        for row in g:

            # Blank row to seperate tables.
            if last_table and row['table'] != last_table:
                w.writerow({})

            w.writerow(row)

            last_table = row['table']

        if isinstance(f, StringIO):
            return f.getvalue()

    @property
    def info(self):
        remotes = ''
        if self.remotes:
            remotes = '\n          '.join([str(x) for x in self.remotes.items()])

        ret = """
------ Library {name} ------
Database: {database}
Cache:    {cache}
Remotes:  {remotes}
            """.format(name=self.name, database=self.database.dsn,
                       cache=self.cache,
                       remotes=remotes)
        return ret

    @property
    def dict(self):

        remotes = [str(r) for r in self.remotes.items()] if self.remotes else []

        manifests = {}
        for f in self.manifests:
            manifests[f.data['uid']] = dict(
                title=f.data['title'],
                partitions=[p.vid for p in f.linked_partitions],
                tables=[t.vid for t in f.linked_tables],
                summary=f.data['summary']['summary_text'],
                stores=[s.ref for s in f.linked_stores])

        stores = {}
        for f in self.stores:
            stores[f.ref] = dict(
                title=f.data['title'],
                summary=f.data['summary'] if f.data['summary'] else '',
                dsn=f.path,
                manifests=[m.ref for m in f.linked_manifests],
                cache=f.data['cache'],
                class_type=f.type_)

        return dict(name=str(self.name),
                    database=str(self.database.dsn),
                    cache=str(self.cache),
                    remotes=remotes,
                    manifests=manifests,
                    stores=stores,
                    # This is the slow one, with about half in setting 'about'.
                    bundles={b.identity.vid: b.summary_dict
                             for b in self.list_bundles()})

    @property
    def summary_dict(self):

        return dict(name=str(self.name),
                    database=str(self.database.dsn),
                    cache=str(self.cache))

    @property
    def schema_dict(self):
        """Represent the entire schema as a dict, suitable for
        conversion to json"""
        s = {}

        for t in self.tables:
            s[t.vid] = t.nonull_col_dict

        return s
