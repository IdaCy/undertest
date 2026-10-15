import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from shared import authority_review as review


class AuthorityReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reviews = self.root / "cohort" / "reviews"
        self.reviews.mkdir(parents=True)
        self.path = self.reviews / "services.v2.json"
        self.raw = b'{"entries":[{"pid":12}],"reviewer":"peer"}\n'
        self.path.write_bytes(self.raw)
        self.pin = self.make_pin(self.path, self.raw)
        self.authority = {"root_authorized": True, "denied_process_review": self.pin,
                          "evidence": [copy.deepcopy(self.pin)]}

    def make_pin(self, path, raw):
        return {"path": str(path.relative_to(self.root)), "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}

    def read(self, authority=None, **kwargs):
        return review.read_selected_review(
            self.authority if authority is None else authority,
            root=self.root, reviews_directory=self.reviews, **kwargs)

    def refused(self, authority=None, **kwargs):
        with self.assertRaises((ValueError, OSError, UnicodeError)):
            self.read(authority, **kwargs)

    def test_absent_selector_preserves_default_without_io(self):
        with mock.patch.object(review, "_read", side_effect=AssertionError("read")):
            self.assertIsNone(self.read({}))
            self.assertIsNone(self.read({"evidence": "legacy shape"}))

    def test_selected_exact_pin_and_repeated_reads(self):
        self.assertEqual(self.read(), (self.path, json.loads(self.raw), self.pin["sha256"]))
        self.path.write_bytes(self.raw.replace(b"12", b"13"))
        self.refused()

    def test_absolute_exact_pin_is_supported(self):
        authority = copy.deepcopy(self.authority)
        authority["denied_process_review"]["path"] = str(self.path)
        authority["evidence"] = [copy.deepcopy(authority["denied_process_review"])]
        self.assertEqual(self.read(authority)[0], self.path)

    def test_null_or_unapproved_selection_never_falls_back(self):
        for value in [None, {}, [], "services.v2.json"]:
            with self.subTest(value=value):
                self.refused({**self.authority, "denied_process_review": value})
        for value in [None, False, 1, "true"]:
            self.refused({**self.authority, "root_authorized": value})

    def test_missing_conflicting_duplicate_or_unknown_evidence(self):
        for evidence in [None, {}, [], [self.pin, self.pin],
                         [{**self.pin, "sha256": "0" * 64}],
                         [{**self.pin, "unexpected": True}],
                         [{"path": self.pin["path"], "sha256": self.pin["sha256"]}],
                         [self.pin, {**self.pin, "path": str(self.path)}]]:
            with self.subTest(evidence=evidence):
                self.refused({**self.authority, "evidence": evidence})

    def test_foreign_and_noncanonical_paths(self):
        for path in ["other/reviews/services.json", "cohort/reviews/../services.json",
                     "cohort/reviews/./services.json", "cohort//reviews/services.json",
                     "cohort/reviews", str(self.root.parent / "outside.json"), ""]:
            pin = {**self.pin, "path": path}
            with self.subTest(path=path):
                self.refused({**self.authority, "denied_process_review": pin,
                              "evidence": [pin]})

    def test_exact_types_hash_and_pre_read_limit(self):
        for change in [{"bytes": True}, {"bytes": -1}, {"bytes": 2**63},
                       {"sha256": "a" * 63}, {"sha256": "A" * 64},
                       {"path": 1}, {"unknown": 1}]:
            pin = {**self.pin, **change}
            with self.subTest(change=change):
                self.refused({**self.authority, "denied_process_review": pin,
                              "evidence": [pin]})
        with mock.patch.object(review.os, "read", side_effect=AssertionError("read")):
            self.refused(max_bytes=len(self.raw) - 1)
            self.path.write_bytes(self.raw + b" ")
            self.refused()

    def test_missing_regular_file_symlink_and_fifo(self):
        self.path.unlink()
        self.refused()
        self.path.mkdir()
        self.refused()
        self.path.rmdir()
        target = self.root / "real.json"
        target.write_bytes(self.raw)
        self.path.symlink_to(target)
        self.refused()
        self.path.unlink()
        os.mkfifo(self.path)
        self.refused()

    def test_symlink_parent_and_cohort_are_refused(self):
        original = self.root / "original"
        self.reviews.rename(original)
        self.reviews.symlink_to(original, target_is_directory=True)
        self.refused()
        self.reviews.unlink()
        original.rename(self.reviews)
        cohort = self.reviews.parent
        new_cohort = self.root / "moved"
        cohort.rename(new_cohort)
        cohort.symlink_to(new_cohort, target_is_directory=True)
        self.refused()

    def test_duplicate_nonfinite_and_nonobject_json(self):
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}',
                    b'{"a":-Infinity}', b'[]', b'null', b'{', b'\xff']:
            self.path.write_bytes(raw)
            pin = self.make_pin(self.path, raw)
            with self.subTest(raw=raw):
                self.refused({**self.authority, "denied_process_review": pin,
                              "evidence": [pin]})

    def test_changed_during_read_or_parent_replacement(self):
        real_read = review.os.read
        changed = False

        def replace_file(fd, size):
            nonlocal changed
            data = real_read(fd, size)
            if not changed:
                changed = True
                self.path.unlink()
                self.path.write_bytes(self.raw)
            return data

        with mock.patch.object(review.os, "read", side_effect=replace_file):
            self.refused()
        changed = False

        def replace_parent(fd, size):
            nonlocal changed
            data = real_read(fd, size)
            if not changed:
                changed = True
                self.reviews.rename(self.root / "old_reviews")
                self.reviews.mkdir()
                self.path.write_bytes(self.raw)
            return data

        with mock.patch.object(review.os, "read", side_effect=replace_parent):
            self.refused()

    def test_direct_pin_read_does_not_claim_authority(self):
        self.assertEqual(review.read_review_pin(
            self.pin, root=self.root, reviews_directory=self.reviews)[1],
            json.loads(self.raw))


if __name__ == "__main__":
    unittest.main()
