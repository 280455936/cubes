# -*- encoding=utf -*-
"""SQL Browser"""

from __future__ import absolute_import

from ..statutils import calculators_for_aggregates, available_calculators
from ..browser import AggregationBrowser, AggregationResult, Drilldown
from ..cells import Cell, PointCut, RangeCut, SetCut
from ..logging import get_logger
from ..errors import ArgumentError, ModelError
from ..stores import Store

from .functions import get_aggregate_function, available_aggregate_functions
from .mapper import SnowflakeMapper, DenormalizedMapper
from .query import StarSchema, QueryContext, to_join, FACT_KEY_LABEL
from .utils import paginate_query, order_query

from ..expressions import collect_dependencies
from .expressions import SQLExpressionCompiler

import collections

try:
    import sqlalchemy
    import sqlalchemy.sql as sql
    from sqlalchemy.sql.expression import and_

except ImportError:
    from ...common import MissingPackage
    sqlalchemy = sql = MissingPackage("sqlalchemy", "SQL aggregation browser")

__all__ = [
    "SQLBrowser",
]


def star_schema_from_cube(cube, metadata, mapper, tables=None):
    """Creates a :class:`StarSchema` instance for `cube` within database
    environment specified by `metadata` using logical to physical `mapper`."""
    # TODO: remove requirement for the mapper, use mapping options and create
    # mapper here

    names = [attr.name for attr in cube.all_attributes]
    mappings = {attr:mapper.physical(attr) for attr in names}

    star = StarSchema(cube.name,
                      metadata,
                      mappings=mappings,
                      fact=mapper.fact_name,
                      joins=cube.joins,
                      tables=tables,
                      schema=mapper.schema
                     )
    return star


class SQLBrowser(AggregationBrowser):
    """SnowflakeBrowser is a SQL-based AggregationBrowser implementation that
    can aggregate star and snowflake schemas without need of having
    explicit view or physical denormalized table.

    Attributes:

    * `cube` - browsed cube
    * `store` - a `Store` object or a SQLAlchemy engine
    * `locale` - locale used for browsing
    * `debug` - output SQL to the logger at INFO level

    Other options in `kwargs`:
    * `metadata` – SQLAlchemy metadata, if `store` is an engine or a
       connection (not a `Store` object)
    * `tables` – tables and/or table expressions used in the star schema
      (refer to the :class:`StarSchema` for more information)
    * `options` - passed to the mapper

    Tuning:

    * `include_summary` - it ``True`` then summary is included in
      aggregation result. Turned on by default.
    * `include_cell_count` – if ``True`` then total cell count is included
      in aggregation result. Turned on by default.
      performance reasons
    * `safe_labels` – safe labelling of the attributes in databases which
      don't allow characters such as ``.`` dots in column names

    Limitations:

    * only one locale can be used for browsing at a time
    * locale is implemented as denormalized: one column for each language

    """

    __options__ = [
        {
            "name": "include_summary",
            "type": "bool"
        },
        {
            "name": "include_cell_count",
            "type": "bool"
        },
        {
            "name": "use_denormalization",
            "type": "bool"
        },
        {
            "name": "safe_labels",
            "type": "bool"
        }

    ]

    def __init__(self, cube, store, locale=None, debug=False, **kwargs):
        """Create a SQL Browser."""

        super(SQLBrowser, self).__init__(cube, store)

        if not cube:
            raise ArgumentError("Cube for browser should not be None.")

        self.logger = get_logger()

        self.cube = cube
        self.locale = locale or cube.locale
        self.debug = debug

        # Database connection and metadata
        # --------------------------------

        if isinstance(store, Store):
            self.connectable = store.connectable
            metadata = store.metadata
        else:
            self.connectable = store

            metadata = kwargs.get("metadata",
                                  sqlalchemy.MetaData(bind=self.connectable))

        # Options
        # -------

        # Merge options with store options
        options = {}
        options.update(store.options)
        options.update(kwargs)

        self.include_summary = options.get("include_summary", True)
        self.include_cell_count = options.get("include_cell_count", True)

        # TODO: Use safe labels
        self.safe_labels = options.get("safe_labels", False)
        self.label_counter = 1

        # Whether to ignore cells where at least one aggregate is NULL
        # TODO: this is undocumented
        # TODO: why is this True and not False by default?
        self.exclude_null_agregates = options.get("exclude_null_agregates",
                                                  True)

        # Mapper
        # ------

        # Mapper is responsible for finding corresponding physical columns to
        # dimension attributes and fact measures. It also provides information
        # about relevant joins to be able to retrieve certain attributes.

        # FIXME: mapper sohuld be a cube-free object with preconfigured naming
        # conventions and should be provided by the store.
        # TODO: change this to is_denormalized
        if options.get("use_denormalization"):
            mapper_class = DenormalizedMapper
        else:
            mapper_class = SnowflakeMapper

        self.logger.debug("using mapper %s for cube '%s' (locale: %s)" %
                          (str(mapper_class.__name__), cube.name, locale))

        # We need mapper just to construct metadata for the star
        mapper = mapper_class(cube, locale=self.locale, **options)

        # TODO: This should include also aggregates if the underlying table is
        # already pre-aggregated
        base = [attr for attr in cube.all_attributes if attr.is_base]
        mappings = {attr.ref:mapper.physical(attr) for attr in base}

        # TODO: include table expressions
        # TODO: I have a feeling that creation of this should belong to the
        # store
        tables = options.get("tables")

        if cube.joins:
            joins = [to_join(join) for join in cube.joins]
        else:
            joins = []

        self.star = StarSchema(self.cube.name,
                               metadata,
                               mappings=mappings,
                               fact=mapper.fact_name,
                               joins=joins,
                               schema=mapper.schema,
                               tables=tables)

        # Attribute Dependencies
        # ----------------------
        #
        # Create attribute dependency map
        #
        all_attributes = cube.all_attributes + cube.aggregates
        self.dependencies = collect_dependencies(all_attributes)

        # Extract hierarchies
        # -------------------
        #
        # This is transition to the future top-level hierarchy objetcs. Also
        # used in the query context which is cubes-objects free
        #
        self.hierarchies = {}
        for dim in cube.dimensions:
            for hier in dim.hierarchies:
                key = (dim.name, hier.name)
                levels = [key.ref for key in hier.keys()]
                #levels = [dim.attribute(key) for key in hier.keys()]

                self.hierarchies[key] = levels

                if dim.default_hierarchy_name == hier:
                    self.hierarchies[(dim.name, None)] = levels

    def features(self):
        """Return SQL features. Currently they are all the same for every
        cube, however in the future they might depend on the SQL engine or
        other factors."""

        features = {
            "actions": ["aggregate", "fact", "members", "facts", "cell"],
            "aggregate_functions": available_aggregate_functions(),
            "post_aggregate_functions": available_calculators()
        }

        return features

    def fact(self, key_value, fields=None):
        """Get a single fact with key `key_value` from cube.

        Number of SQL queries: 1."""

        statement = self.denormalized_statement(attributes=fields,
                                                include_fact_key=True)
        condition = statement.columns[FACT_KEY_LABEL] == key_value
        statement = statement.where(condition)

        cursor = self.execute(statement, "fact")
        labels = cursor.keys()
        row = cursor.fetchone()

        if row:
            # Convert SQLAlchemy object into a dictionary
            # TODO: safe labels
            record = self.row_to_dict(row, labels)
        else:
            record = None

        cursor.close()

        return record

    def facts(self, cell=None, fields=None, order=None, page=None,
              page_size=None):
        """Return all facts from `cell`, might be ordered and paginated.

        Number of SQL queries: 1.
        """
        cell = cell or Cell(self.cube)

        statement = self.denormalized_statement(cell=cell, attributes=fields)
        statement = paginate_query(statement, page, page_size)
        # TODO: use natural order
        statement = order_query(statement,
                                order,
                                natural_order={},
                                labels=None)

        cursor = self.execute(statement, "facts")

        # TODO: safe labels
        return ResultIterator(cursor, cursor.keys())

    # TODO: requires rewrite
    def test(self, aggregate=False, **options):
        """Tests whether the statement can be constructed."""
        raise NotImplementedError("Queued for refactoring")
        cell = Cell(self.cube)

        attributes = self.cube.all_attributes

        builder = QueryBuilder(self)
        statement = builder.denormalized_statement(cell,
                                                   attributes)
        statement = statement.limit(1)
        result = self.connectable.execute(statement)
        result.close()

        if aggregate:
            result = self.aggregate()

    # TODO: requires rewrite
    def provide_members(self, cell, dimension, depth=None, hierarchy=None,
                        levels=None, attributes=None, page=None,
                        page_size=None, order=None):
        """Return values for `dimension` with level depth `depth`. If `depth`
        is ``None``, all levels are returned.

        Number of database queries: 1.
        """
        raise NotImplementedError("Queued for refactoring")
        if not attributes:
            attributes = []
            for level in levels:
                attributes += level.attributes

        builder = QueryBuilder(self)
        builder.members_statement(cell, attributes)
        builder.paginate(page, page_size)
        builder.order(order)

        result = self.execute(builder.statement, "members")

        return ResultIterator(result, builder.labels)

    # TODO: requires rewrite
    def path_details(self, dimension, path, hierarchy=None):
        """Returns details for `path` in `dimension`. Can be used for
        multi-dimensional "breadcrumbs" in a used interface.

        Number of SQL queries: 1.
        """
        raise NotImplementedError("Queued for refactoring")
        dimension = self.cube.dimension(dimension)
        hierarchy = dimension.hierarchy(hierarchy)

        cut = PointCut(dimension, path, hierarchy=hierarchy)
        cell = Cell(self.cube, [cut])

        attributes = []
        for level in hierarchy.levels[0:len(path)]:
            attributes += level.attributes

        builder = QueryBuilder(self)
        builder.denormalized_statement(cell,
                                       attributes,
                                       include_fact_key=True)
        builder.paginate(0, 1)
        cursor = self.execute(builder.statement,
                                        "path details")

        row = cursor.fetchone()

        if row:
            member = dict(zip(builder.labels, row))
        else:
            member = None

        return member

    def execute(self, statement, label=None):
        """Execute the `statement`, optionally log it. Returns the result
        cursor."""
        self._log_statement(statement, label)
        return self.connectable.execute(statement)

    def is_builtin_function(self, function_name, aggregate):
        # FIXME: return actual truth
        return True

    # TODO: requires rewrite
    def provide_aggregate(self, cell, aggregates, drilldown, split, order,
                          page, page_size, across=None, **options):
        """Return aggregated result.

        Arguments:

        * `cell`: cell to be aggregated
        * `measures`: aggregates of these measures will be considered
        * `aggregates`: aggregates to be considered
        * `drilldown`: list of dimensions or list of tuples: (`dimension`,
          `hierarchy`, `level`)
        * `split`: an optional cell that becomes an extra drilldown segmenting
          the data into those within split cell and those not within
        * `attributes`: list of attributes from drilled-down dimensions to be
          returned in the result
        * `across`: list of other cubes to be drilled across

        Query tuning:

        * `include_cell_count`: if ``True`` (``True`` is default) then
          `result.total_cell_count` is
          computed as well, otherwise it will be ``None``.
        * `include_summary`: if ``True`` (default) then summary is computed,
          otherwise it will be ``None``

        Result is paginated by `page_size` and ordered by `order`.

        Number of database queries:

        * without drill-down: 1 – summary
        * with drill-down (default): 3 – summary, drilldown, total drill-down
          record count

        Notes:

        * measures can be only in the fact table

        """

        # TODO: implement reminder

        # TODO: implement drill-across
        if across:
            raise NotImplementedError("Drill-across is not yet implemented")

        result = AggregationResult(cell=cell, aggregates=aggregates)

        # TODO: remove unnecessary parts of the following discussion once
        # implemented and documented

        # Discussion:
        # -----------
        # the only diference between the summary statement and non-summary
        # statement is the inclusion of the group-by clause

        # Summary
        # -------

        if self.include_summary or not (drilldown or split):
            statement = self.aggregation_statement(cell,
                                                   aggregates=aggregates,
                                                   drilldown=drilldown,
                                                   for_summary=True)

            cursor = self.execute(statement, "aggregation summary")
            row = cursor.fetchone()

            # TODO: use builder.labels
            if row:
                # Convert SQLAlchemy object into a dictionary
                labels = [col.name for col in statement.columns]
                record = dict(zip(labels, row))
            else:
                record = None

            cursor.close()
            result.summary = record


        # Drill-down
        # ----------
        #
        # Note that a split cell if present prepends the drilldown

        if drilldown or split:
            if not (page_size and page is not None):
                self.assert_low_cardinality(cell, drilldown)

            result.levels = drilldown.result_levels(include_split=bool(split))

            natural_order = drilldown.natural_order
            # TODO: add natural order of aggregates

            self.logger.debug("preparing drilldown statement")

            statement = self.aggregation_statement(cell,
                                                   aggregates=aggregates,
                                                   drilldown=drilldown)
            # TODO: look the order_query spec for arguments
            # TODO: use safe labels too
            statement = paginate_query(statement, page, page_size)
            statement = order_query(statement,
                                    order,
                                    natural_order,
                                    labels=None)

            cursor = self.execute(statement, "aggregation drilldown")

            #
            # Find post-aggregation calculations and decorate the result
            #
            result.calculators = calculators_for_aggregates(self.cube,
                                                            aggregates,
                                                            drilldown,
                                                            split,
                                                            available_aggregate_functions())
            # TODO: safe labels
            labels = [col.name for col in statement.columns]
            result.cells = ResultIterator(cursor, labels)
            result.labels = labels

            if self.include_cell_count:
                # TODO: we want to get unpaginated number of records here
                count_statement = statement.alias().count()
                row_count = self.execute(count_statement).fetchone()
                total_cell_count = row_count[0]
                result.total_cell_count = total_cell_count

        elif result.summary is not None:
            # Do calculated measures on summary if no drilldown or split
            # TODO: should not we do this anyway regardless of
            # drilldown/split?
            calculators = calculators_for_aggregates(self.cube,
                                                     aggregates,
                                                    drilldown,
                                                    split,
                                                    available_aggregate_functions())
            for calc in calculators:
                calc(result.summary)

        # If exclude_null_aggregates is True then don't include cells where
        # at least one of the bult-in aggregates is NULL
        if result.cells is not None and self.exclude_null_agregates:
            afuncs = available_aggregate_functions()
            aggregates = [agg for agg in aggregates if not agg.function or agg.function in afuncs]
            names = [str(agg) for agg in aggregates]
            result.exclude_if_null = names

        return result

    def _create_context(self, attributes, cell, drilldown=None):
        attributes = attributes or []
        cell_attributes = cell.key_attributes or []

        all_attributes = attributes + cell_attributes

        if drilldown:
            all_attributes += drilldown.all_attributes

        return QueryContext(self.star,
                            attributes=[str(attr) for attr in all_attributes],
                            dependencies=self.dependencies,
                            hierarchies=self.hierarchies,
                            parameters=None,     # not yet
                            label="cube {}".format(self.cube.name))

    def denormalized_statement(self, attributes=None, cell=None,
                               include_fact_key=False):
        """Returns a statement representing denormalized star restricted by
        `cell`. If `attributes` is not specified, then all cube's attributes
        are selected."""

        attributes = attributes or self.cube.all_attributes
        context = self._create_context(attributes, cell)

        if include_fact_key:
            selection = [self.star.fact_key_column]
        else:
            selection = []

        if attributes:
            names = [attr.ref for attr in attributes]
            selection += context.columns(names)

        if cell:
            cell_condition = context.condition_for_cell(cell)
        else:
            cell_condition = None

        star = context.star.get_star(attributes)
        statement = sql.expression.select(selection,
                                          from_obj=star,
                                          whereclause=cell_condition)

        return statement

    def aggregation_statement(self, cell, aggregates, drilldown=None,
                              split=None, for_summary=False, across=None):
        """Builds a statement to aggregate the `cell`.

        * `cell` – `Cell` to aggregate
        * `aggregates` – list of aggregates to consider (should not be empty)
        * `drilldown` – an optional `Drilldown` object
        * `split` – split cell for split condition
        * `for_summary` – do not perform `GROUP BY` for the drilldown. The
          drilldown is used only for choosing tables to join
        * `across` – cubes that share dimensions
        """
        # TODO: `across` should be used here
        # TODO: PTD
        # TODO: semiadditive

        if across:
            raise NotImplementedError("Drill-across is not yet implemented")

        if not aggregates:
            raise ArgumentError("List of aggregates sohuld not be empty")

        drilldown = drilldown or Drilldown()

        self.logger.debug("prepare aggregation statement. cell: '%s' "
                          "drilldown: '%s' for summary: %s" %
                          (",".join([str(cut) for cut in cell.cuts]),
                          drilldown, for_summary))

        select_attributes = drilldown.all_attributes

        # Gather all attributes involved in the aggregation.
        all_attributes = cell.key_attributes
        all_attributes += select_attributes

        # Now we need to determine all base attributes. Some of the attributes
        # might be derived through an expression and the expression might
        # contain attributes not present in the original list. Therefore we
        # need to provide list of all potential attributes to be used,
        # constants and variables ("parameters").

        # XXX

        if split:
            all_attributes += split.all_attributes

        # JOIN
        # ----

        base = [attr.ref for attr in all_attributes if attr.is_base]
        star = self.star.get_star(base)

        # Drilldown – Group-by
        # --------------------
        #
        # SELECT – Prepare the master selection
        #     * master drilldown items

        selection = [self.attribute_column(a) for a in set(drilldown.all_attributes)]

        # SPLIT
        # -----
        if split:
            split_column = self._split_cell_column(split)
            selection.append(split_column)

        # WHERE
        # -----
        condition = self.condition_for_cell(cell)

        group_by = selection[:] if not for_summary else None

        # TODO: insert semiadditives here
        aggregate_cols = [self.aggregate_column(aggr) for aggr in aggregates]

        if for_summary:
            # Don't include the group-by part (see issue #157 for more
            # information)
            selection = aggregate_cols
        else:
            selection += aggregate_cols

        statement = sql.expression.select(selection,
                                          from_obj=star,
                                          use_labels=True,
                                          whereclause=condition,
                                          group_by=group_by)

        return statement

    def aggregate_column(self, aggregate, coalesce_measure=False):
        """Returns an expression that performs the aggregation of attribute
        `aggregate`. The result's label is the aggregate's name.  `aggregate`
        has to be `MeasureAggregate` instance.

        If aggregate function is post-aggregation calculation, then `None` is
        returned.

        Aggregation function names are case in-sensitive.

        If `coalesce_measure` is `True` then selected measure column is wrapped
        in ``COALESCE(column, 0)``.
        """
        # TODO: support aggregate.expression

        if not (aggregate.expression or aggregate.function):
            raise ModelError("Neither expression nor function specified for "
                             "aggregate {} in cube {}"
                             .format(aggregate, self.cube.name))

        if aggregate.expression:
            raise NotImplementedError("Expressions are not yet implemented")

        function_name = aggregate.function.lower()
        function = get_aggregate_function(function_name)

        if not function:
            raise NotImplementedError("I don't know what to do")
            # Original statement:
            return None

        # TODO: this below for FactCountFucntion
        # context = dict(self.base_columns)
        # context["__fact_key__"] = self.attribute_column(self.fact_key)
        expression = function(aggregate, self.base_columns, coalesce_measure)

        return expression

    def _log_statement(self, statement, label=None):
        label = "SQL(%s):" % label if label else "SQL:"
        self.logger.debug("%s\n%s\n" % (label, str(statement)))


    def row_to_dict(self, row, labels):
        """Converts a result `row` into a dictionary. Applies proper key
        labels. Main purpose of this method is to make sure that safe labels
        (labels without dots or special characters) are converted back to user
        specified labels."""

        if self.safe_labels:
            raise NotImplementedError
        else:
            return dict(zip(labels, row))


class ResultIterator(object):
    """
    Iterator that returns SQLAlchemy ResultProxy rows as dictionaries
    """
    def __init__(self, result, labels):
        self.result = result
        self.batch = None
        self.labels = labels
        self.exclude_if_null = None

    def __iter__(self):
        while True:
            if not self.batch:
                many = self.result.fetchmany()
                if not many:
                    break
                self.batch = collections.deque(many)

            row = self.batch.popleft()

            if self.exclude_if_null \
                    and any(cell[agg] is None for agg in self.exclude_if_nul):
                continue

            yield dict(zip(self.labels, row))
