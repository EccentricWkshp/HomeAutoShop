"""
Reading a code table off a manual page, whatever shape the publisher used.

Every fixture here is written by hand to mirror a real page's *structure* with
invented wording, which is both the honest way to test somebody else's document
and the better test: it fails on the shape rather than on a description
happening to change.

The three shapes are the ones that cover most of the library, and each broke a
different assumption:

* **VAG** puts two code columns side by side, `SAE Code` and `VAG Code`, and
  the second is a dash. Counting one column across from the code gave 295 rows
  reading `-`.
* **GM** groups a row — one cell naming four codes — and runs all four
  definitions together in the description beside it.
* **Ford** does not put descriptions on the page at all. It is an index of 929
  codes, each linking to its own page.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from . import manuals

VAG = """
<table>
 <tr><th>SAE Code</th><th>VAG Code</th><th>Code Description</th><th>Corrective action</th></tr>
 <tr><td>P0420</td><td>-</td><td>Catalyst efficiency below threshold</td><td>Inspect the converter</td></tr>
 <tr><td>P0300</td><td>16123</td><td>Random misfire detected</td><td>Check the plugs</td></tr>
</table>
"""

GM = """
<table>
 <tr><th>DTC</th><th>Description</th></tr>
 <tr><td>DTC P0601, P0603 or P0604</td>
     <td>DTC P0601 Read only memory failure DTC P0603 Long term memory reset
         DTC P0604 Random access memory failure</td></tr>
 <tr><td>DTC P0634</td><td>DTC P0634 Control module overtemperature</td></tr>
</table>
"""

FORD = """
<ul>
 <li><a href='Descriptions/P Codes/P0010/'>P0010</a>
 <li><a href='Descriptions/P Codes/P0011/'>P0011</a>
 <li><a href='Descriptions/B Codes/B1318/'>B1318</a>
</ul>
"""


class TheColumnIsFoundNotCountedTests(SimpleTestCase):
    def test_the_description_column_is_read_off_the_header(self):
        found = manuals.read(VAG)

        self.assertEqual(found.shape, "table")
        self.assertEqual(found.codes["P0420"], "Catalyst efficiency below threshold")

    def test_the_second_code_column_is_not_mistaken_for_the_description(self):
        """`VAG Code` names a code and sits exactly where a positional rule
        would look. The tie is settled on the data: the column that actually
        carries codes is the code column."""
        found = manuals.read(VAG)
        self.assertNotIn("-", found.codes.values())
        self.assertNotIn("16123", found.codes.values())

    def test_corrective_action_is_not_a_description(self):
        """It is advice about the repair. Putting it in the field that says
        what a code *means* would be a different claim entirely."""
        self.assertNotIn("Inspect the converter", manuals.read(VAG).codes.values())

    def test_a_table_with_no_header_still_reads(self):
        found = manuals.read(
            "<table><tr><td>P0420</td><td>Catalyst efficiency low</td></tr></table>"
        )
        self.assertEqual(found.codes["P0420"], "Catalyst efficiency low")


class AGroupedRowTests(SimpleTestCase):
    def test_each_code_in_the_group_gets_its_own_definition(self):
        """Handing the whole cell to all four gives four rows that each say
        what the other three mean as well."""
        found = manuals.read(GM)

        self.assertEqual(found.codes["P0601"], "Read only memory failure")
        self.assertEqual(found.codes["P0603"], "Long term memory reset")
        self.assertEqual(found.codes["P0604"], "Random access memory failure")

    def test_the_label_the_description_opens_with_is_dropped(self):
        """GM writes `DTC P0634 Control module overtemperature`. Leaving the
        prefix makes every row start by repeating what you looked up."""
        self.assertEqual(manuals.read(GM).codes["P0634"], "Control module overtemperature")

    def test_an_ordinary_row_is_not_split(self):
        found = manuals.read(
            "<table><tr><th>Code</th><th>Description</th></tr>"
            "<tr><td>P0420</td><td>Catalyst below threshold, see P0430 as well</td></tr></table>"
        )
        self.assertEqual(found.codes["P0420"], "Catalyst below threshold, see P0430 as well")


class AnIndexWithNoDefinitionsTests(SimpleTestCase):
    def test_the_codes_are_recorded_as_undefined_rather_than_as_blank(self):
        """"This make has a code P0010" and "this is what P0010 means on this
        make" are different facts, and only the second is what a lookup is
        for. Storing the first as though it were the second is the failure
        this whole design refuses."""
        found = manuals.read(FORD)

        self.assertEqual(found.shape, "index")
        self.assertEqual(found.codes, {})
        self.assertEqual(set(found.undefined), {"P0010", "P0011", "B1318"})

    def test_it_keeps_the_link_so_the_definition_can_be_fetched(self):
        self.assertIn("P0010", manuals.read(FORD).undefined["P0010"])

    def test_a_list_that_does_define_its_codes_is_read_as_definitions(self):
        found = manuals.read("<ul><li>P0420 Catalyst efficiency below threshold</ul>")
        self.assertEqual(found.codes["P0420"], "Catalyst efficiency below threshold")
        self.assertEqual(found.undefined, {})


class NothingUnreadableIsKeptTests(SimpleTestCase):
    """A harvested dash is the same lie as an invented definition, arrived at
    by machine. §8.3c refuses the first because an operator acts on it."""

    def test_a_dash_is_not_a_definition(self):
        found = manuals.read(
            "<table><tr><th>Code</th><th>Description</th></tr>"
            "<tr><td>P0420</td><td>-</td></tr></table>"
        )
        self.assertEqual(found.codes, {})
        self.assertEqual(found.dropped, 1)

    def test_the_code_repeated_back_is_not_a_definition(self):
        found = manuals.read(
            "<table><tr><th>Code</th><th>Description</th></tr>"
            "<tr><td>P0420</td><td>P0420</td></tr></table>"
        )
        self.assertEqual(found.codes, {})
        self.assertEqual(found.dropped, 1)

    def test_punctuation_is_not_a_definition(self):
        found = manuals.read(
            "<table><tr><th>Code</th><th>Description</th></tr>"
            "<tr><td>P0420</td><td>--- / ---</td></tr></table>"
        )
        self.assertEqual(found.codes, {})

    def test_a_page_with_no_codes_yields_nothing_rather_than_guessing(self):
        found = manuals.read("<p>Service precautions for this vehicle.</p>")
        self.assertFalse(found)
        self.assertEqual(found.shape, "none")

    def test_a_navigation_page_of_links_is_not_a_code_table(self):
        """The DTC index on a GM vehicle is a page of links to a page per
        system. It names no codes and must not look like a harvest."""
        found = manuals.read(
            "<ul><li><a href='Antilock Brakes - DTC/'>Antilock Brakes - DTC</a>"
            "<li><a href='Transmission - DTC/'>Transmission - DTC</a></ul>"
        )
        self.assertFalse(found)


class LooksLikeADescriptionTests(SimpleTestCase):
    """The shapes that survive every other test and still say nothing.

    Each of these came out of one library harvest as a stored definition:
    10,429 reserved rows, 277 sentences and 70 fragments out of 151,128. The
    structure is real; the wording is invented, as everywhere else in this file.
    """

    def _row(self, code, text, header="Description"):
        return manuals.read(
            f"<table><tr><th>Code</th><th>{header}</th></tr>"
            f"<tr><td>{code}</td><td>{text}</td></tr></table>"
        )

    def test_a_reserved_row_is_not_a_definition(self):
        """A manufacturer chart marks every number the standard has not
        assigned. It is true about the code and empty about its meaning."""
        self.assertEqual(self._row("P215E", "ISO/SAE Reserved").codes, {})

    def test_the_other_ways_of_saying_nothing_are_refused_too(self):
        for nothing in ("Reserved", "Not detected", "Not used", "N/A",
                        "No description available", "Future use",
                        "Manufacturer Controlled DTC", "ISO/SAE Controlled"):
            with self.subTest(nothing):
                self.assertEqual(self._row("P0770", nothing).codes, {})

    def test_naming_the_half_of_the_numbering_a_code_sits_in_is_not_a_meaning(self):
        """`dtc.parse` works that out from the number, and STRUCTURE says it in
        more words. Stored as a make's definition it would sit in front of the
        better answer rather than beside it."""
        self.assertEqual(self._row("P3200", "Manufacturer Controlled DTC").codes, {})

    def test_a_definition_that_merely_mentions_control_is_kept(self):
        """The rule is anchored, so it cannot eat a real one."""
        found = self._row("P3401", "Cylinder 1 Deactivation Control Circuit Open")

        self.assertEqual(
            found.codes["P3401"], "Cylinder 1 Deactivation Control Circuit Open"
        )

    def test_prose_about_a_code_is_not_the_meaning_of_that_code(self):
        """A bullet that opens with a code and continues a sentence. Taking
        whatever follows made `is for the right sunload sensor.` a definition."""
        found = manuals.read("<ul><li>B0188 is for the right sunload sensor.</ul>")

        self.assertEqual(found.codes, {})

    def test_a_code_named_only_in_prose_is_still_recorded_as_named(self):
        """Refusing the wording is not the same as refusing the fact. The page
        does say this make has a B0188."""
        found = manuals.read("<ul><li>B0188 is for the right sunload sensor.</ul>")

        self.assertIn("B0188", found.undefined)

    def test_the_tail_of_a_sentence_cut_at_the_first_code_is_refused(self):
        """A bullet naming several codes, read as a definition of the first.
        What is left after its own code is removed opens with the comma that
        separated it from the next, which is the evidence it was cut."""
        found = manuals.read("<ul><li>P0461, P0462, and P0463 are Type B DTCs.</ul>")

        self.assertEqual(found.codes, {})
        self.assertIn("P0461", found.undefined)

    def test_a_definition_that_names_another_code_is_kept(self):
        """The limit of the rule above, and the reason it is not tidier: this
        really is what the code means."""
        found = self._row("C1293", "C1291 or C1292 set in a previous cycle")

        self.assertEqual(found.codes["C1293"], "C1291 or C1292 set in a previous cycle")

    def test_a_definition_that_merely_starts_lowercase_is_kept(self):
        """`invalid Data Received From Image Processing Module` is a real
        definition, so an opening lowercase letter cannot be the test."""
        found = self._row("U053B", "invalid Data Received From Image Module")

        self.assertEqual(found.codes["U053B"], "invalid Data Received From Image Module")

    def test_a_capitalised_preposition_opens_a_definition_not_a_sentence(self):
        found = self._row("B1234", "In Vehicle Temperature Sensor Circuit")

        self.assertEqual(found.codes["B1234"], "In Vehicle Temperature Sensor Circuit")

    def test_a_trailing_semicolon_is_furniture(self):
        found = self._row("C0235", "Rear Wheel Speed Signal Circuit Open;")

        self.assertEqual(found.codes["C0235"], "Rear Wheel Speed Signal Circuit Open")


class EnableConditionsAreNotADescriptionTests(SimpleTestCase):
    """VAG publishes seven columns and only the second says what a code means.

    `Secondary Parameters with Enable Conditions` matched the bare word
    `condition`, so 195 Audi codes were defined by the thresholds under which
    they set while the definition sat unread one column over.
    """

    VAG7 = """
    <table>
     <tr><th>DTC</th><th>Error Message</th><th>Diagnostic Procedure</th>
         <th>Malfunction Criteria and Threshold Value</th>
         <th>Secondary Parameters with Enable Conditions</th>
         <th>Monitoring Time Length</th><th>Frequency of checks, MIL Illum</th></tr>
     <tr><td>P000A</td><td>Intake Camshaft Position Slow Response Bank 1</td>
         <td>Check the camshaft adjustment valve</td>
         <td>Slow response 7-50 KW</td>
         <td>Engine speed, 0 RPM ECT, -10.5 C Time after start, 5 s</td>
         <td>3.5 Seconds</td><td>2 DCY</td></tr>
    </table>
    """

    def test_the_description_column_wins_over_the_conditions_column(self):
        found = manuals.read(self.VAG7)

        self.assertEqual(
            found.codes["P000A"], "Intake Camshaft Position Slow Response Bank 1"
        )

    def test_a_column_headed_conditions_is_still_read_when_it_is_the_only_one(self):
        """`condition` stays in the header list: a two-column table that titles
        its description `Condition` is a shape another library really uses."""
        found = manuals.read(
            "<table><tr><th>DTC</th><th>Condition</th></tr>"
            "<tr><td>P0420</td><td>Catalyst efficiency below threshold</td></tr></table>"
        )

        self.assertEqual(found.codes["P0420"], "Catalyst efficiency below threshold")
