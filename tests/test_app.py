import gzip
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import flameprof

import app as app_module


class CProfileRegressionTests(unittest.TestCase):
    def test_cprofile_conversion_handles_zero_caller_cumtime(self):
        func_a = ("a.py", 1, "A")
        func_b = ("b.py", 2, "B")
        stats = {
            func_a: (1, 1, 1.0, 1.0, {func_b: (1, 1, 0.0, 0.0)}),
            func_b: (1, 1, 0.0, 0.0, {}),
        }

        with self.assertRaises(ZeroDivisionError):
            flameprof.render(stats, io.StringIO(), "log")

        self.assertEqual(
            app_module._cprofile_stats_to_folded_stacks(stats),
            b"a.py:1:A 1000000\n",
        )

    def test_prof_gz_request_succeeds_for_problematic_stats(self):
        func_a = ("a.py", 1, "A")
        func_b = ("b.py", 2, "B")
        stats = {
            func_a: (1, 1, 1.0, 1.0, {func_b: (1, 1, 0.0, 0.0)}),
            func_b: (1, 1, 0.0, 0.0, {}),
        }

        client = app_module.app.test_client()

        with (
            patch.object(
                app_module,
                "_download_bytes",
                return_value=(gzip.compress(b"placeholder"), None),
            ),
            patch.object(app_module.pstats, "Stats", return_value=SimpleNamespace(stats=stats)),
            patch.object(app_module, "_run_inferno_flamegraph", return_value=(b"<svg/>", None)),
        ):
            response = client.get("/?url=https://example.com/profile.prof.gz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"<svg/>")


if __name__ == "__main__":
    unittest.main()
