import unittest
import traceback
import os

from cubes import Workspace
from cubes.errors import BrowserError
import cubes.browser
from cubes import compat


@unittest.skipIf(compat.py3k, "Not testing slicer for Python 3 due to some "
                              "flask errors.")
@unittest.skipIf("TEST_SLICER" not in os.environ,
                 "No TEST_SLICER environment variable set.")


class SlicerTestCase(unittest.TestCase):
    def setUp(self):
        self.w = Workspace()
        self.w.add_slicer("myslicer", "http://localhost:5010",
                          username=os.environ.get("SLICER_USERNAME"),
                          password=os.environ.get("SLICER_PASSWORD"))

        self.cube_list = self.w.list_cubes()

    def first_date_dim(self, cube):
        for d in cube.dimensions:
            if ( d.info.get('is_date') ):
                return d
        raise BrowserError("No date dimension in cube %s" % cube.name)

    def test_basic(self):
        for c in self.cube_list:
            if c.get('category') is not None and 'Mix' in c.get('category', ''):
                continue

            cube = self.w.cube(c.get('name'))
            date_dim = self.first_date_dim(cube)
            cut = cubes.browser.RangeCut(date_dim, [ 2013, 9, 25 ], None)
            cell = cubes.browser.Cell(cube, [ cut ])
            drill_levels = [ l for l in date_dim.hierarchy().levels if l.name in ('day', 'date') ]
            if not drill_levels:
                continue

            drill = cubes.browser.Drilldown([(date_dim, None, date_dim.level(drill_levels[0]))], cell)
            b = self.w.browser(cube)
            try:
                attr_dim = cube.dimension("attr")
                split = cubes.browser.PointCut(attr_dim, ['paid', 'pnb'])
            except:
                split = None
            try:
                kw = {}
                if cube.aggregates:
                    kw['aggregates'] = [cube.aggregates[0]]
                elif cube.measures:
                    kw['measures'] = [ cube.measures[0] ]
                else:
                    raise ValueError("Cube has neither aggregates nor measures")
                result = b.aggregate(cell, drilldown=drill, split=split, **kw)
            except:
                traceback.print_exc()
