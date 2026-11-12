"""자세히 보기 창: ‘문제: 대상’ 목록을 문제별로 묶어 보이는 글 (group_issue_lines)."""
from app.qt.dialogs import group_issue_lines


def test_groups_by_problem_in_first_seen_order():
    out = group_issue_lines(["A 문제: x.pdf", "B 문제: y.pdf", "A 문제: z.pdf", "콜론 없음"])
    assert out == "A 문제 (2건)\n  - x.pdf\n  - z.pdf\n\nB 문제 (1건)\n  - y.pdf\n\n기타 (1건)\n  - 콜론 없음"


def test_lines_without_colon_go_under_misc():
    assert group_issue_lines(["A: 1", "A: 2", "가", "나"]).endswith("기타 (2건)\n  - 가\n  - 나")


def test_single_target_still_has_header():
    assert group_issue_lines(["일부만 읽음: a.pdf (3장 중 1장)"]) == "일부만 읽음 (1건)\n  - a.pdf (3장 중 1장)"
    assert group_issue_lines(["a.pdf: 읽음 3장", "b.pdf: 읽음 1장"]) == \
        "a.pdf (1건)\n  - 읽음 3장\n\nb.pdf (1건)\n  - 읽음 1장"
    assert group_issue_lines([]) == ""


def test_splits_only_plain_key_lists():
    assert group_issue_lines(["아이콘은 넣지 않음: a, b, c"]) == "아이콘은 넣지 않음 (3건)\n  - a\n  - b\n  - c"
    # 다른 글이 섞인 대상은 쪼개지 않는다
    assert group_issue_lines(["용어 불일치(기록만): k: 불, 물"]) == "용어 불일치(기록만) (1건)\n  - k: 불, 물"
    assert group_issue_lines(["여러 원문이 함께 쓰는 번역: '불' ← 火, 炎"]) == \
        "여러 원문이 함께 쓰는 번역 (1건)\n  - '불' ← 火, 炎"
