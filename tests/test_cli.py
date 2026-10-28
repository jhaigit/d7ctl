from d7ctl.cli import main


def run(capsys, *args):
    code = main(["-p", "emulator", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_info(capsys):
    code, out, _ = run(capsys, "info")
    assert code == 0 and "TH-D7G" in out and "Band B" in out


def test_tune_and_freq(capsys):
    code, out, _ = run(capsys, "tune", "A", "freq=145.525", "shift=-", "offset=0.6", "tone=100")
    assert code == 0 and "145.52500" in out and "tone 100.0" in out
    code, out, _ = run(capsys, "freq", "B", "438.5", "--step", "25")
    assert code == 0 and "438.50000" in out and "step 25k" in out


def test_mem_commands(capsys, tmp_path):
    code, out, _ = run(capsys, "mem", "list", "0-30")
    assert code == 0 and "W1AW RPT" in out and "ODD SPLT" in out
    code, out, _ = run(capsys, "mem", "write", "50", "freq=147.12", "shift=+", "offset=0.6", "name=NEW")
    assert code == 0 and "NEW" in out and "147.12000" in out
    code, out, err = run(capsys, "mem", "write", "60", "name=X")
    assert code == 1 and "freq=" in err
    csv = tmp_path / "m.csv"
    code, out, _ = run(capsys, "mem", "export", str(csv))
    assert code == 0 and csv.exists()
    code, out, _ = run(capsys, "mem", "import", str(csv), "--dry-run")
    assert code == 0 and "W1AW RPT" in out


def test_settings_cli(capsys):
    code, out, _ = run(capsys, "setting", "set", "APO", "30 min")
    assert code == 0 and "APO = 30 min" in out
    code, out, err = run(capsys, "setting", "set", "APO", "never")
    assert code == 1 and "choose one of" in err
    code, out, _ = run(capsys, "setting", "list", "--group", "APRS")
    assert code == 0 and "My call sign" in out
    assert main(["setting", "describe"]) == 0


def test_raw_and_tx(capsys):
    code, out, _ = run(capsys, "raw", "ID", "XYZ")
    assert code == 0 and "ID TH-D7G" in out and "?" in out
    code, out, _ = run(capsys, "tx", "--seconds", "0.1")
    assert code == 0 and "back to receive" in out
    code, out, err = run(capsys, "tx", "--seconds", "999")
    assert code == 1


def test_backup_restore(capsys, tmp_path):
    path = tmp_path / "b.json"
    code, out, _ = run(capsys, "backup", str(path))
    assert code == 0 and "memories" in out
    code, out, _ = run(capsys, "restore", str(path))
    assert code == 0 and out.startswith("restored")


def test_bad_port(capsys):
    assert main(["-p", "/dev/does-not-exist", "info"]) == 1
