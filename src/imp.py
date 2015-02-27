# -*- coding: utf-8 -*-

#   Copyright (c) 2010-2014, MIT Probabilistic Computing Project
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

# XXX This module would be called `import', but Python won't let me
# use Python keywords as module names.

import itertools
import math

import bayeslite.core as core

from bayeslite.sqlite3_util import sqlite3_quote_name
from bayeslite.util import casefold
from bayeslite.util import unique

# XXX The schema language here, such as it is, is pretty limited.
# Perhaps generating the SQL schema is the wrong approach here.  When
# this refactoring is done, perhaps we ought to separate
#
# (a) guessing a SQL schema and BQL generator from data, and
# (b) importing data from external formats into an existing table.
#
# Sometimes you want to do both at once: given a CSV file with header,
# guess a SQL schema and BQL generator and import it into a table.

def bayesdb_import_generated(bdb, table, generator, column_types=None,
        ifnotexists=False):
    if ifnotexists:
        with bdb.savepoint():
            if not core.bayesdb_table_exists(bdb, table):
                column_names, rows = generator()
                _bayesdb_import(bdb, table, column_names, rows, column_types)
    else:
        column_names, rows = generator()
        _bayesdb_import(bdb, table, column_names, rows, column_types)

def bayesdb_import(bdb, table, column_names, rows, column_types=None,
        ifnotexists=False):
    if ifnotexists:
        with bdb.savepoint():
            if not core.bayesdb_table_exists(bdb, table):
                _bayesdb_import(bdb, table, column_names, rows, column_types)
    else:
        _bayesdb_import(bdb, table, column_names, rows, column_types)

def _bayesdb_import(bdb, table, column_names, rows, column_types):
    # Map the case-folded names to their positions.
    column_names_folded = {}
    for i, name in enumerate(column_names):
        if casefold(name) in column_names_folded:
            raise IOError("Column names differ only in case: %s, %s" %
                (column_names[column_names_folded[casefold(name)]], name))
        column_names_folded[casefold(name)] = i
    assert len(column_names_folded) == len(column_names)

    # Determine the column types.
    if column_types is None:
        column_types = bayesdb_import_guess_column_types(column_names, rows)
    else:
        if len(column_types) != len(column_names):
            raise IOError("Imported data has %d columns, expected %d" %
                (len(column_names), len(column_types)))
        for name in column_types:
            if casefold(name) not in column_names_folded:
                # XXX Ignore this column?  Treat as numerical?  Infer?
                raise IOError("Imported data has unknown column: %s" % (name,))

    # Determine the non-ignored columns.
    column_types_folded = dict((casefold(name), column_types[name])
        for name in column_types)
    nonignored_indices = []
    for i, name in enumerate(column_names):
        if column_types_folded[casefold(name)] == "ignore":
            continue
        nonignored_indices.append(i)
    nonignored_column_names = [column_names[i] for i in nonignored_indices]
    nonignored_column_types = dict((name, column_types[name])
        for name in column_types if column_types[name] != "ignore")
    nonignored_column_types_folded = dict((name, column_types_folded[name])
        for name in column_types_folded
        if column_types_folded[name] != "ignore")

    # Generate the SQL schema.
    ncols = len(nonignored_column_names)
    assert ncols == len(nonignored_column_types)
    qt = sqlite3_quote_name(table)
    table_def = bayesdb_table_definition(table, nonignored_column_names,
        nonignored_column_types_folded)

    # Instantiate the SQL schema and import the data.
    with bdb.savepoint():
        bdb.sql_execute(table_def)
        qcns = ",".join(map(sqlite3_quote_name, nonignored_column_names))
        qcps = ",".join("?" * ncols)
        insert_sql = "INSERT INTO %s (%s) VALUES (%s)" % (qt, qcns, qcps)
        for row in rows:
            munged_row = []
            for i, name in zip(nonignored_indices, nonignored_column_names):
                column_type = column_types_folded[casefold(name)]
                value = None
                if column_type in bayesdb_import_mungers:
                    value = bayesdb_import_mungers[column_type](row[i])
                else:
                    value = row[i]
                munged_row.append(value)
            bdb.sql_execute(insert_sql, munged_row)
        core.bayesdb_import_sqlite_table(bdb, table, nonignored_column_names,
            nonignored_column_types)

bayesdb_import_mungers = {
    "numerical": lambda v: float('NaN' if v == '' else v),
}

def bayesdb_table_definition(table, column_names, column_types_folded):
    column_defs = [bayesdb_column_definition(name,
            column_types_folded[casefold(name)])
        for name in column_names]
    qt = sqlite3_quote_name(table)
    return ("CREATE TABLE %s (%s)" % (qt, ",".join(column_defs)))

bayesdb_column_type_to_sqlite_type = \
    dict((ct, sql) for ct, _cont_p, sql, _mt in core.bayesdb_type_table)
def bayesdb_column_definition(column_name, column_type):
    qcn = sqlite3_quote_name(column_name)
    sqlite_type = bayesdb_column_type_to_sqlite_type[column_type]
    qualifiers = []
    if column_type == "key":
        # XXX SQLite3 quirk: PRIMARY KEY does not imply NOT NULL.
        qualifiers.append("NOT NULL PRIMARY KEY")
    separator = " " if qualifiers else ""
    return ("%s %s%s%s" % (qcn, sqlite_type, separator, " ".join(qualifiers)))

#### Imported column heuristicking

# XXX This logic should not be duplicated for importing external data
# vs creating generative models for SQL tables.  However, we need to
# make slightly different decisions for the two cases.  Blah blah blah
# blah blah.

def bayesdb_import_guess_column_types(column_names, rows):
    column_types = {}
    need_key = True
    for i, name in enumerate(column_names):
        column_types[name] = \
            bayesdb_import_guess_column_type(rows, i, need_key)
        if column_types[name] == "key":
            need_key = False
    return column_types

# XXX Pass count_cutoff/ratio_cutoff through from above.
def bayesdb_import_guess_column_type(rows, i, may_be_key,
        count_cutoff=20, ratio_cutoff=0.02):
    if may_be_key and bayesdb_import_column_keyable_p(rows, i):
        return "key"
    elif bayesdb_import_column_numerical_p(rows, i, count_cutoff,
            ratio_cutoff):
        return "numerical"
    else:
        return "categorical"

def bayesdb_import_column_keyable_p(rows, i):
    if bayesdb_import_column_integerable_p(rows, i):
        return len(rows) == len(unique([int(row[i]) for row in rows]))
    elif not bayesdb_import_column_floatable_p(rows, i):
        # XXX Is unicode(...) necessary?  I think they should all be
        # strings here.  Where can stripping happen?
        assert all(row[i] == unicode(row[i]).strip() for row in rows)
        return len(rows) == len(unique([row[i] for row in rows]))
    else:
        return False

def bayesdb_import_column_integerable_p(rows, i):
    try:
        for row in rows:
            if unicode(row[i]) != unicode(int(row[i])):
                return False
    except ValueError:
        return False
    return True

def bayesdb_import_column_floatable_p(rows, i):
    try:
        for row in rows:
            # We treat an empty string as missing and equivalent to NaN.
            if row[i] == '':
                continue
            float(row[i])
    except ValueError:
        return False
    return True

def bayesdb_import_column_numerical_p(rows, i, count_cutoff, ratio_cutoff):
    if not bayesdb_import_column_floatable_p(rows, i):
        return False
    ndistinct = len(unique([float(row[i]) for row in rows
        if row[i] != '' and not math.isnan(float(row[i]))]))
    if ndistinct <= count_cutoff:
        return False
    ndata = len(rows)
    if (float(ndistinct) / float(ndata)) <= ratio_cutoff:
        return False
    return True
