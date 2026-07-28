"""Regression tests for permit_scan.py.

Run from the repo root:      python -m unittest discover -s tests -v
Or just:                     python tests/test_permit_scan.py

These cover the pure, importable parts: form classification, field extraction,
address/SBL normalization, Laserfiche path building, the county reconcile
guardrails, and the staging-filename rules. No API calls, no OCR, no GUI.

Where a test encodes a KNOWN LIMITATION rather than desired behavior, it says so
in the test name or a comment — those are the ones to revisit, not to trust.
"""
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import permit_scan as ps


# ── Form classification ───────────────────────────────────────────────────────

class TestDetectPermitType(unittest.TestCase):
    def test_official_building_permit(self):
        self.assertEqual(ps.detect_permit_type(
            "BUILDING PERMIT\nTOWN OF YORKTOWN\nBUILDING DEPARTMENT"), "official")

    def test_official_survives_ocr_double_spacing(self):
        # Scanned PDFs produce doubled whitespace; a plain substring match missed these
        self.assertEqual(ps.detect_permit_type(
            "BUILDING  PERMIT\nTOWN  OF  YORKTOWN"), "official")

    def test_application_beats_official_markers(self):
        # Application forms also carry "BUILDING PERMIT" + Yorktown letterhead
        self.assertEqual(ps.detect_permit_type(
            "APPLICATION FOR BUILDING PERMIT\nTOWN OF YORKTOWN"), "application")

    def test_application_all_permit_types(self):
        for kind in ("DEMOLITION", "ELECTRICAL", "PLUMBING", "POOL", "FENCE", "SIGN"):
            self.assertEqual(
                ps.detect_permit_type(f"APPLICATION FOR A {kind} PERMIT"), "application",
                f"{kind} application misclassified")

    def test_application_with_ocr_merged_spacing(self):
        # Quick-pass Tesseract reads the stylized header as "FORA" (commit ac41ca5)
        self.assertEqual(ps.detect_permit_type(
            "APPLICATION FORA DEMOLITION PERMIT"), "application")

    def test_application_via_office_use_only_fallback(self):
        # Structural fallback when the header itself is unreadable
        self.assertEqual(ps.detect_permit_type(
            "(Office use only)  Application No. 2010-0656"), "application")

    def test_inspection_procedure_sheet_is_not_official(self):
        # Carries "Building Permit" + letterhead; false-positiving as official
        # ended the page sweep before a real permit deeper in the batch (184d7d3)
        self.assertEqual(ps.detect_permit_type(
            "INSPECTION PROCEDURE\nBuilding Permit\nTOWN OF YORKTOWN"), "unknown")

    def test_unrelated_text_is_unknown(self):
        self.assertEqual(ps.detect_permit_type("certificate of insurance"), "unknown")


# ── Permit / application numbers ──────────────────────────────────────────────

class TestFindPermitNumber(unittest.TestCase):
    def test_plain_permit_number(self):
        self.assertEqual(ps.find_permit_number("Permit No. 20100240"), "20100240")

    def test_orange_folder_label(self):
        self.assertEqual(ps.find_permit_number("BLDG. PER No. 20110110"), "20110110")

    def test_type_suffix_is_kept(self):
        self.assertEqual(ps.find_permit_number("Permit No. 20100027DEMO"), "20100027DEMO")

    def test_unknown_glued_suffix_is_kept(self):
        # Real scan 2026-07-27: FD wasn't whitelisted, so 20160001FD was stored
        # as 20160001 and collided with the real permit 20160001 the next day
        self.assertEqual(
            ps.find_permit_number("Permit #: 20160001FD File Date: 2/26/2016"),
            "20160001FD")

    def test_spaced_following_word_is_not_a_suffix(self):
        # "File" follows with a space — it's the next label, not part of the ID
        self.assertEqual(
            ps.find_permit_number("Permit #: 20160001 File Date: 1/4/2016"),
            "20160001")

    def test_glued_mixed_case_word_is_not_a_suffix(self):
        # A dropped space before a normal word must not be swallowed
        self.assertEqual(
            ps.find_permit_number("Permit #: 20160001File Date: 1/4/2016"),
            "20160001")

    def test_hyphenated_value_is_rejected(self):
        # Application numbers are hyphenated; permit numbers never are. The dash
        # check must run BEFORE digit-stripping or the guard is defeated.
        self.assertEqual(ps.find_permit_number("Application No. 2010-0656"), "")
        self.assertEqual(ps.find_permit_number("Permit No: 2010-0656"), "")

    def test_must_be_exactly_eight_digits(self):
        self.assertEqual(ps.find_permit_number("Permit No. 1234"), "")
        self.assertEqual(ps.find_permit_number("Permit No. 1234567890123"), "")

    def test_blocked_digits_skipped(self):
        self.assertEqual(
            ps.find_permit_number("Permit No. 20100656", blocked_digits="20100656"), "")

    def test_first_match_wins(self):
        self.assertEqual(
            ps.find_permit_number("Permit No. 20100240 and Permit No. 20100241"), "20100240")


class TestFindApplicationNumber(unittest.TestCase):
    def test_hyphenated_form(self):
        self.assertEqual(ps.find_application_number("Application No. 2010-0656"), "2010-0656")

    def test_hash_form(self):
        self.assertEqual(ps.find_application_number("APPLICATION #2016-0783"), "2016-0783")

    def test_absent(self):
        self.assertEqual(ps.find_application_number("no application number here"), "")


# ── SBL ───────────────────────────────────────────────────────────────────────

class TestFindSbl(unittest.TestCase):
    def test_assembled_format(self):
        self.assertEqual(ps.find_sbl("SBL 48.11-1-11"), "48.11-1-11")

    def test_ocr_comma_period_artifact(self):
        self.assertEqual(ps.find_sbl("16,.10-4-25"), "16.10-4-25")

    def test_separate_section_block_lot_fields(self):
        self.assertEqual(ps.find_sbl("SECTION 6.17 BLOCK 2 LOT(S) 3"), "6.17-2-3")

    def test_dash_read_as_decimal_point(self):
        # OCR routinely reads the section's "." as "-" on handwritten forms
        self.assertEqual(ps.find_sbl("SEC. 27-10 BLK. 3 LOT 34"), "27.10-3-34")

    def test_absent(self):
        self.assertEqual(ps.find_sbl("nothing to see"), "")

    def test_KNOWN_LIMITATION_condo_unit_part_is_truncated(self):
        # A 4-part condo key loses its unit suffix here. Not currently a problem:
        # parcel_lookup_sbl accepts a base lot whose units exist (prefix match),
        # so '15.16-1-21' still resolves. Revisit if unit-level filing is needed.
        self.assertEqual(ps.find_sbl("15.16-1-21.1-2"), "15.16-1-21")


class TestSblCandidates(unittest.TestCase):
    def test_dropped_decimal_point(self):
        self.assertIn("59.10-1-4", ps._sbl_candidates("5910-1-4"))

    def test_dropped_zero_padding(self):
        # Official sections always carry a 2-digit fraction
        cands = ps._sbl_candidates("16.6-1-3")
        self.assertIn("16.06-1-3", cands)
        self.assertIn("16.60-1-3", cands)

    def test_as_read_form_is_tried_first(self):
        self.assertEqual(ps._sbl_candidates("5910-1-4")[0], "5910-1-4")

    def test_leading_zeros_stripped(self):
        self.assertEqual(ps._sbl_candidates("016.06-1-3"), ["16.06-1-3"])

    def test_already_valid_needs_no_repair(self):
        self.assertEqual(ps._sbl_candidates("48.11-1-11"), ["48.11-1-11"])


# ── Address ───────────────────────────────────────────────────────────────────

class TestFindAddress(unittest.TestCase):
    def test_printed_permit_location_label(self):
        self.assertEqual(ps.find_address("Location: 1005 East Main St"), "1005 EAST MAIN ST")

    def test_orange_folder_label(self):
        self.assertEqual(ps.find_address("LOCATION OF PROJECT: 3871 PERRY ST"), "3871 PERRY ST")

    def test_application_form_label(self):
        self.assertEqual(ps.find_address("ADDRESS/LOCATION OF PROPERTY: 395 Saber Ct"),
                         "395 SABER CT")

    def test_owner_mailing_address_is_never_the_job_site(self):
        # "present address of owner" must stay out of SITE_LABELS
        self.assertEqual(ps.find_address("Present Address of Owner: 190 East Main St"), "")

    def test_building_department_address_ignored(self):
        self.assertEqual(ps.find_address("Location: 363 Underhill Avenue"), "")

    def test_town_state_zip_tail_trimmed(self):
        self.assertEqual(
            ps.find_address("Location: 1005 East Main St, Yorktown Heights, NY 10598"),
            "1005 EAST MAIN ST")

    def test_inspection_record_job_line(self):
        self.assertEqual(ps.find_address("Job: Smith, 459 Crow Hill Road, new deck"),
                         "459 CROW HILL ROAD")


class TestAddressNormalization(unittest.TestCase):
    def test_suffix_spelled_out_is_abbreviated(self):
        self.assertEqual(ps.normalize_suffix("1005 EAST MAIN STREET"), "1005 EAST MAIN ST")
        self.assertEqual(ps.normalize_suffix("CROW HILL ROAD"), "CROW HILL RD")

    def test_split_number_from_street(self):
        self.assertEqual(ps.split_address("3871 PERRY ST"), ("3871", "PERRY ST"))

    def test_street_with_no_number(self):
        self.assertEqual(ps.split_address("SABER COURT"), ("", "SABER CT"))

    def test_only_trailing_suffix_is_normalized(self):
        # "ST" mid-name (route designators etc.) must survive untouched
        self.assertEqual(ps.split_address("1903 EAST MAIN ST RT 6"),
                         ("1903", "EAST MAIN ST RT 6"))


class TestFuzzyMatchStreet(unittest.TestCase):
    """Cutoff is 0.9. Lowering it to 0.8 makes confident WRONG corrections."""

    def test_repairs_real_ocr_damage(self):
        self.assertEqual(ps.fuzzy_match_street("CROW HILL RO"), "CROW HILL RD")

    def test_does_not_snap_heywood_to_wood(self):
        self.assertEqual(ps.fuzzy_match_street("HEYWOOD ST"), "HEYWOOD ST")

    def test_does_not_swap_hickory_suffix(self):
        self.assertEqual(ps.fuzzy_match_street("HICKORY LN"), "HICKORY LN")

    def test_spelled_out_suffix_is_normalized_before_it_gets_here(self):
        self.assertEqual(ps.fuzzy_match_street(ps.normalize_suffix("SABER COURT")), "SABER CT")

    def test_exact_match_passes_through(self):
        self.assertEqual(ps.fuzzy_match_street("PERRY ST"), "PERRY ST")


class TestTrimSiteAddress(unittest.TestCase):
    def test_wrapped_line_and_comma_after_number(self):
        self.assertEqual(ps._trim_site_address("395,\nSaber Court, Yorktown Heights NY 10598"),
                         "395 Saber Court")

    def test_stops_at_street_suffix(self):
        self.assertEqual(ps._trim_site_address("12 Main St Apt 3"), "12 Main St")

    def test_clean_address_unchanged(self):
        self.assertEqual(ps._trim_site_address("2955 CURRY STREET"), "2955 CURRY STREET")


class TestFallbackPageRegexes(unittest.TestCase):
    def test_plan_review_anchor(self):
        m = ps._PLAN_REVIEW_ADDR_RE.search(
            "PLAN REVIEW LIST FOR CONSTRUCTION PROPOSED AT 395, Saber Court")
        self.assertIsNotNone(m)
        self.assertEqual(ps._trim_site_address(m.group(1)), "395 Saber Court")

    def test_plan_review_alternate_wording(self):
        m = ps._PLAN_REVIEW_ADDR_RE.search("the work proposed at 2955 Curry Street")
        self.assertIsNotNone(m)

    def test_electrical_cert_detected(self):
        for header in ("BOARD OF FIRE UNDERWRITERS", "BUREAU OF ELECTRICITY",
                       "ELECTRICAL INSPECTION"):
            self.assertIsNotNone(ps._ELEC_CERT_RE.search(header), header)

    def test_insurance_cert_is_not_an_electrical_cert(self):
        self.assertIsNone(ps._ELEC_CERT_RE.search("CERTIFICATE OF LIABILITY INSURANCE"))


# ── Laserfiche paths ──────────────────────────────────────────────────────────

class TestLaserfichePath(unittest.TestCase):
    ROOT = r"TownOfYorktown\Building Department\Parcels"

    def test_abbreviated_suffix_gets_a_period(self):
        self.assertEqual(ps.laserfiche_path_for("3871", "PERRY ST"),
                         rf"{self.ROOT}\P\PERRY ST.\3871 PERRY ST.")

    def test_spelled_out_ending_gets_no_period(self):
        self.assertEqual(ps.laserfiche_path_for("1005", "OLD COUNTRY WAY"),
                         rf"{self.ROOT}\O\OLD COUNTRY WAY\1005 OLD COUNTRY WAY")

    def test_first_word_is_never_dotted(self):
        # "LA VOIE CT" starts with LA as a real word, not an abbreviation
        self.assertEqual(ps.laserfiche_path_for("12", "LA VOIE CT"),
                         rf"{self.ROOT}\L\LA VOIE CT.\12 LA VOIE CT.")

    def test_mid_name_suffix_is_dotted(self):
        self.assertEqual(ps.laserfiche_path_for("7", "MOHANSIC AVE EAST"),
                         rf"{self.ROOT}\M\MOHANSIC AVE. EAST\7 MOHANSIC AVE. EAST")

    def test_no_street_number_uses_the_street_as_leaf(self):
        # DO NOT "fix" this to emit a leading space. Laserfiche is inconsistent
        # for numberless parcels — both spellings were confirmed by hand:
        #     ...\D\DARBY ST.\DARBY ST.          (no space)
        #     ...\S\SAGAMORE AVE.\ SAGAMORE AVE. (orphaned space from "{num} {st}")
        # The line has already been changed in BOTH directions (87eb301 removed
        # the space; permit 20160001 on 2026-07-27 showed it is sometimes needed).
        # Neither form is right for every parcel, so the app emits the no-space
        # form and _refresh_path/_copy_path warn that the folder may start with
        # a space. ~7% of Yorktown parcels (1,005 of 14,407) have no number.
        self.assertEqual(ps.laserfiche_path_for("", "DARBY ST"),
                         rf"{self.ROOT}\D\DARBY ST.\DARBY ST.")

    def test_no_street_number_on_a_route(self):
        self.assertEqual(ps.laserfiche_path_for("", "ROUTE 6"),
                         rf"{self.ROOT}\R\ROUTE 6\ROUTE 6")

    def test_empty_street_yields_no_path(self):
        self.assertEqual(ps.laserfiche_path_for("123", ""), "")


# ── County reconcile: the trust guardrails ────────────────────────────────────

@unittest.skipUnless(ps.parcel_db_available(),
                     "yorktown_parcels.db not present — run build_parcel_db.py")
class TestReconcileWithParcels(unittest.TestCase):
    """The rules that stop a confident-wrong value reaching the form."""

    def setUp(self):
        self.logs = []

    def run_reconcile(self, num, street, sbl):
        sources = {"permit": "", "address": "", "sbl": ""}
        num, street, sbl, sources = ps.reconcile_with_parcels(
            num, street, sbl, sources, self.logs.append)
        return num, street, sbl, sources

    def logged(self, fragment):
        return any(fragment in m for m in self.logs)

    def test_sbl_filled_from_address(self):
        _n, _s, sbl, sources = self.run_reconcile("3871", "PERRY ST", "")
        self.assertEqual(sbl, "6.17-2-3")
        self.assertEqual(sources["sbl"], "parcel")

    def test_address_filled_from_sbl(self):
        num, street, _sbl, sources = self.run_reconcile("", "", "6.17-2-3")
        self.assertEqual((num, street), ("3871", "PERRY ST"))
        self.assertEqual(sources["address"], "parcel")

    def test_matching_pair_is_marked_verified(self):
        *_, sources = self.run_reconcile("3871", "PERRY ST", "6.17-2-3")
        self.assertTrue(sources.get("verified"))
        self.assertTrue(self.logged("Verified against county parcels"))

    def test_uncorroborated_suffix_match_is_NOT_applied(self):
        # The ac41ca5 guardrail. A misread '6.17-3-3' uniquely suffix-matches
        # 16.17-3-3 (1474 CHRISTINE RD) — but nothing on the form corroborates
        # it, so the field must be left alone rather than "repaired" to a
        # confidently wrong parcel. This exact failure shipped once.
        _n, _s, sbl, _src = self.run_reconcile("3871", "ZORNE", "6.17-3-3")
        self.assertEqual(sbl, "6.17-3-3", "reconcile invented an unconfirmed parcel")
        self.assertTrue(self.logged("nothing read off the"))

    def test_reconcile_always_reports_an_outcome(self):
        # Silent paths made it look like the county check was never running
        self.run_reconcile("3269", "STO", "")
        self.assertTrue(self.logs, "reconcile returned without logging anything")

    def test_nothing_extracted_still_logs(self):
        self.run_reconcile("", "", "")
        self.assertTrue(self.logs)


class TestCountyResolves(unittest.TestCase):
    def test_empty_sbl_never_resolves(self):
        self.assertIsNone(ps._county_resolves(""))

    @unittest.skipUnless(ps.parcel_db_available(), "parcel db not present")
    def test_real_parcel_resolves(self):
        self.assertIsNotNone(ps._county_resolves("6.17-2-3"))

    @unittest.skipUnless(ps.parcel_db_available(), "parcel db not present")
    def test_garbage_does_not_resolve(self):
        self.assertIsNone(ps._county_resolves("999.99-9-9"))


# ── Staging filenames (added 2026-07-27) ──────────────────────────────────────

class TestFinalNameRecognition(unittest.TestCase):
    """Telling an already-confirmed file from raw scanner output. Getting this
    wrong folds the previous batch's filed PDF into the next one."""

    def test_confirmed_names_match(self):
        for name in ("20160009 OPEN.pdf", "20160009 CLOSED.pdf",
                     "20160009DEMO OPEN.pdf", "20160009 OPEN - 2.pdf"):
            self.assertTrue(ps._FINAL_NAME_RE.match(name), name)

    def test_raw_scanner_output_does_not_match(self):
        for name in ("20260722153247958.pdf", "20160009.pdf",
                     "scan of 20160009 OPEN.pdf", "plans.tif"):
            self.assertFalse(ps._FINAL_NAME_RE.match(name), name)


class TestFreeFinalPath(unittest.TestCase):
    """Confirm & Rename must never overwrite a confirmed file still awaiting
    its drag into Laserfiche."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "20160009 OPEN.pdf")
        self.other = os.path.join(self.tmp, "20260722153247958.pdf")
        open(self.other, "w").close()
        self.free = ps.App._free_final_path.__get__(object())  # uses no self state

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_collision_keeps_the_name(self):
        self.assertEqual(self.free(self.target, [{"current": self.other}]), self.target)

    def test_collision_with_a_prior_filing_picks_the_next_free_name(self):
        open(self.target, "w").close()
        self.assertEqual(
            os.path.basename(self.free(self.target, [{"current": self.other}])),
            "20160009 OPEN - 2.pdf")

    def test_collision_with_our_own_input_may_be_overwritten(self):
        # A re-merge legitimately includes the previously merged file
        open(self.target, "w").close()
        self.assertEqual(
            self.free(self.target, [{"current": self.target}, {"current": self.other}]),
            self.target)

    def test_counts_past_names_already_taken(self):
        open(self.target, "w").close()
        open(os.path.join(self.tmp, "20160009 OPEN - 2.pdf"), "w").close()
        self.assertEqual(
            os.path.basename(self.free(self.target, [{"current": self.other}])),
            "20160009 OPEN - 3.pdf")


class TestStageableExtensions(unittest.TestCase):
    def test_scan_output_is_accepted(self):
        for ext in (".pdf", ".tif", ".tiff", ".png", ".jpg"):
            self.assertIn(ext, ps._STAGE_EXTS, ext)

    def test_everything_else_is_ignored(self):
        for ext in (".docx", ".txt", ".db", ".xlsx"):
            self.assertNotIn(ext, ps._STAGE_EXTS, ext)


# ── Source ranking ────────────────────────────────────────────────────────────

class TestSourceRank(unittest.TestCase):
    def test_county_outranks_everything(self):
        top = max(ps._SOURCE_RANK.values())
        self.assertEqual(ps._SOURCE_RANK["parcel"], top)

    def test_claude_outranks_tesseract_on_handwriting(self):
        self.assertGreater(ps._SOURCE_RANK["claude"], ps._SOURCE_RANK["tesseract_hw"])

    def test_printed_tesseract_outranks_claude(self):
        self.assertGreater(ps._SOURCE_RANK["tesseract"], ps._SOURCE_RANK["claude"])

    def test_fallback_tiers_can_never_displace_anything(self):
        # elec_cert / plan_review / history are fill-only by construction:
        # _apply_extracted requires a STRICTLY higher rank to overwrite
        lowest = min(v for k, v in ps._SOURCE_RANK.items() if k)
        for src in ("elec_cert", "plan_review", "history", "tesseract_hw"):
            self.assertEqual(ps._SOURCE_RANK[src], lowest, src)

    def test_every_rank_has_a_ui_label(self):
        for src in ps._SOURCE_RANK:
            if src:
                self.assertIn(src, ps._SRC_STYLE, f"{src} has no _SRC_STYLE entry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
