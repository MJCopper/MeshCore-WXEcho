from app.meshcore_discovery import list_usb_serial_devices


def test_lists_every_by_id_entry_in_name_order(tmp_path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    seeed = by_id / "usb-Seeed_Studio_XIAO_nRF52840_B89FC3F98AFD92B1-if00"
    other = by_id / "usb-Other_Device-if00"
    seeed.symlink_to(tmp_path / "ttyACM0")
    other.symlink_to(tmp_path / "ttyUSB0")

    assert list_usb_serial_devices(by_id) == [str(other), str(seeed)]


def test_lists_entries_without_probing_or_filtering(tmp_path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    unrelated = by_id / "usb-Other_Device-if00"
    unrelated.symlink_to(tmp_path / "missing-target")

    assert list_usb_serial_devices(by_id) == [str(unrelated)]


def test_missing_by_id_directory_returns_empty_list(tmp_path):
    assert list_usb_serial_devices(tmp_path / "missing") == []
