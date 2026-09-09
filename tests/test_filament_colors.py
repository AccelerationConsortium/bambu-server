from bambu_server.backend import AmsTrayReading
from bambu_server.config import TrayColorLabel
from bambu_server.filament_colors import tray_color_label


def test_transparency_requires_a_matching_operator_declaration():
    declaration = TrayColorLabel(ams_id=128, tray_id=0, material="PC", reported_color="00000000", name="Transparent")
    tray = AmsTrayReading(ams_id=128, tray_id=0, tray_type="PC", tray_color="00000000")
    assert tray_color_label(tray) == ("Unknown color", "unknown")
    assert tray_color_label(tray, [declaration]) == ("Transparent", "operator_declared")
    for changed in [
        AmsTrayReading(ams_id=129, tray_id=0, tray_type="PC", tray_color="00000000"),
        AmsTrayReading(ams_id=128, tray_id=0, tray_type="PLA", tray_color="00000000"),
        AmsTrayReading(ams_id=128, tray_id=0, tray_type="PC", tray_color="FFFFFFFF"),
    ]:
        assert tray_color_label(changed, [declaration])[0] != "Transparent"


def test_color_matches_are_not_spool_identity():
    assert tray_color_label(AmsTrayReading(tray_type="PLA", tray_color="FFFFFFFF")) == ("Jade White", "bambu_color_match")
    assert tray_color_label(AmsTrayReading(tray_type="PP", tray_color="FFFFFFFF")) == ("White", "generic")
    assert tray_color_label(AmsTrayReading(tray_type="PLA", tray_color="F72323FF")) == ("Red", "generic")
