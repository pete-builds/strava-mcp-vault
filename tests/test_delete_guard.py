from formatters import format_delete_activities

def test_formatter_handles_empty_ids():
    out = format_delete_activities(0, [])
    assert "No IDs provided" in out

def test_formatter_counts_not_found():
    out = format_delete_activities(1, [1,2,3])
    assert "Deleted" in out and "Not found" in out

if __name__ == '__main__':
    test_formatter_handles_empty_ids()
    test_formatter_counts_not_found()
    print('PASS: delete guard tests')
