from src.usernames import extract_shortcode, normalize_username, parse_follower_count


def test_normalize_clean():
    assert normalize_username("heynana_o3o") == "heynana_o3o"
    assert normalize_username("  Rosyfit__ ") == "rosyfit__"


def test_normalize_decorations():
    assert normalize_username("*heynana_o3o") == "heynana_o3o"
    assert normalize_username("**__1_7_4mon**") == "__1_7_4mon"
    assert normalize_username("@someuser") == "someuser"


def test_normalize_trailing_memo():
    assert normalize_username("a_aj_j  / 계약 취소") == "a_aj_j"
    assert normalize_username("ann.heidi.mc( healthylife.heidi)") == "ann.heidi.mc"
    assert normalize_username("do.roc.y") == "do.roc.y"


def test_normalize_invalid():
    assert normalize_username(None) is None
    assert normalize_username("") is None
    assert normalize_username("둥둥피치") is None
    assert normalize_username("한글이름 abc") is None


def test_shortcode():
    assert extract_shortcode("https://www.instagram.com/p/DJ3rrhIR2VC/?img_index=1") == "DJ3rrhIR2VC"
    assert extract_shortcode("https://www.instagram.com/reel/ABC12345/") == "ABC12345"
    assert extract_shortcode("https://instagram.com/reels/xYz_-1234") == "xYz_-1234"
    assert extract_shortcode("https://www.instagram.com/tv/QwErTy123/") == "QwErTy123"
    assert extract_shortcode("https://www.instagram.com/someuser/p/AbCdEf987/") == "AbCdEf987"


def test_shortcode_not_a_post():
    assert extract_shortcode("https://www.instagram.com/heynana_o3o/") is None
    assert extract_shortcode("https://youtu.be/abc") is None
    assert extract_shortcode(None) is None
    assert extract_shortcode("") is None


def test_follower_count():
    assert parse_follower_count("19000") == 19000
    assert parse_follower_count("19,000") == 19000
    assert parse_follower_count("1.9만") == 19000
    assert parse_follower_count("19k") == 19000
    assert parse_follower_count("1.2m") == 1200000
    assert parse_follower_count("") is None
    assert parse_follower_count(None) is None
    assert parse_follower_count("약 2만명") is None
