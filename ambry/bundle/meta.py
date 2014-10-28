"""Metadata objects for a bundle

Copyright (c) 2013 Clarinova. This file is licensed under the terms of the
Revised BSD License, included in this distribution as LICENSE.txt
"""

from ..util.meta import *

class About(DictGroup):

    title = ScalarTerm()
    license = ScalarTerm()
    rights = ScalarTerm()
    subject = ScalarTerm()
    summary = ScalarTerm()

    rights = ScalarTerm()
    tags = ListTerm()
    groups = ListTerm()

class Documentation(DictGroup):

    readme = ScalarTerm()
    main = ScalarTerm()

class ContactTerm(DictTerm):

    name = ScalarTerm(store_none=False)
    email = ScalarTerm(store_none=False)
    url = ScalarTerm(store_none=False)

    def __nonzero__(self):
        return bool(self.name or self.email or self.url)

    def __bool__(self):
        return self.__nonzero__()

class Contact(DictGroup):
    """ """
    creator = ContactTerm()
    maintainer = ContactTerm()


class Identity(DictGroup):
    """ """
    dataset = ScalarTerm()
    id = ScalarTerm()
    revision = ScalarTerm()
    source = ScalarTerm()
    subset = ScalarTerm()
    variation = ScalarTerm()
    btime = ScalarTerm()
    bspace = ScalarTerm()
    type = ScalarTerm()
    version = ScalarTerm()

class Names(DictGroup):
    """Names that are generated from the identity"""

    fqname = ScalarTerm()
    name = ScalarTerm()
    vid = ScalarTerm()
    vname = ScalarTerm()


class SourceTerm(DictTerm):
    url = ScalarTerm()
    description = ScalarTerm(store_none=False)
    dd_url = ScalarTerm(store_none=False) # Data Dictitionary URL
    file = ScalarTerm(store_none=False) # A name or regex to extract from a multi-file ZIP
    comment = ScalarTerm(store_none=False)  # Just a comment

    def __nonzero__(self):
        return bool(self.url or self.file or self.description or self.dd_url)

    def __bool__(self):
        return self.__nonzero__()


class Sources(TypedDictGroup):
    """References to source URLS"""
    _proto = SourceTerm()


class Dependencies(VarDictGroup):
    """Bundle dependencies"""

class Build(VarDictGroup):
    """Build parameters"""

class Extract(VarDictGroup):
    """Extract parameters"""

class Views(VarDictGroup):
    """Extract parameters"""

class Process(VarDictGroup):
    """Process data. Build times, etc."""

class ExtDocTerm(DictTerm):
    url = ScalarTerm()
    title = ScalarTerm()
    description = ScalarTerm()
    source = ScalarTerm()

class ExtDoc(TypedDictGroup):
    """External Documentation"""
    _proto = ExtDocTerm() # Reusing

class VersonTerm(DictTerm):
    """Version Description"""
    version = ScalarTerm()
    description = ScalarTerm(store_none=False)

class Versions(TypedDictGroup):
    """Names that are generated from the identity"""
    _proto = VersonTerm()


class Top(Metadata):

    _non_term_file = 'meta/build.yaml'


    about = About(file='bundle.yaml')
    contact_source = Contact(file='bundle.yaml')
    contact_bundle = Contact(file='bundle.yaml')
    sources = Sources(file='meta/build.yaml')
    dependencies = Dependencies(file='meta/build.yaml')
    identity = Identity(file='bundle.yaml')
    names = Names(file='bundle.yaml')
    build = Build(file='meta/build.yaml')
    extract = Extract(file='meta/build.yaml')
    views = Views(file='meta/build.yaml')
    external_documentation = ExtDoc(file='bundle.yaml')
    documentation = Documentation(file='meta/doc.yaml')
    versions = Versions(file='bundle.yaml')
    process = Process()

