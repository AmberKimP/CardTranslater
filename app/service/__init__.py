"""화면과 상관없는 작업 흐름 (명령줄·웹·PySide6가 모두 이것을 부른다).

규칙 (PRD 6장 '화면과 엔진 나누기'):
- 이 모듈과 app/의 다른 모듈은 화면 라이브러리(PySide6, FastAPI 등)를 import하지 않고, print하지 않는다.
- 긴 작업은 progress 콜백으로 진행률을 알리고, cancel 토큰을 쪽마다 확인한다.
  콜백은 작업 스레드에서 불린다. 화면 쪽이 자기 스레드로 넘긴다
  (PySide6: Signal.emit, 웹: 큐/SSE). 콜백 안에서 화면을 직접 건드리지 않는다.
- 결과는 dataclass로 돌려준다 (경로, 경고, 상태). 화면은 이것만 보고 그린다.
- 사용자에게 물어야 하는 것(엑셀 확인 항목 등)은 예외가 아니라 결과 상태로 돌려준다.
  화면은 물어본 뒤 accept_issues=True로 다시 부른다.

    erase(project_dir, pages="1-30")                  -> EraseResult   (7단계, cleared/)
    export_excel(project_dir)                          -> Path
    compose(project_dir, excel=..., accept_issues=...) -> ComposeResult (8단계, translated/)

이 패키지는 화면이 부르는 이름을 모두 여기서 내보낸다. 실제 코드는 주제별 하위 모듈(_extract, _glossary …)에 있다.
하위 모듈끼리는 `모듈.이름`으로 서로 부른다. `service.이름 = 값`(시험의 monkeypatch)은 아래 _ServiceModule이
같은 것을 가진 하위 모듈에도 넣으므로, 예전처럼 service에서 바꿔 끼우면 된다.
"""

from __future__ import annotations

import sys
import types

import numpy as np  # noqa: F401

from app.version import VERSION  # noqa: F401 (화면이 버전을 service로 읽는다)
# 예전 service.py가 불러 두던 엔진 이름들 (service.find_claude처럼 쓰거나 시험에서 바꿔 끼운다)
from app import applog, report  # noqa: F401
from app.claude_setup import find_claude  # noqa: F401
from app.erase import erase_page, find_background_image, load_image_rgb, render_on_image_grid  # noqa: F401
from app.project import ensure_not_source, load_project, parse_page_spec, sha256_file  # noqa: F401
from app.typeset_compose import safe_name  # noqa: F401
from app.service import _base  # noqa: F401
from app.service import _erase_compose  # noqa: F401
from app.service import _extract  # noqa: F401
from app.service import _glossary  # noqa: F401
from app.service import _translate_erase  # noqa: F401
from app.service import _overview  # noqa: F401
from app.service import _fonts  # noqa: F401
from app.service import _regions  # noqa: F401
from app.service import _settings  # noqa: F401
from app.service import _cells_icons  # noqa: F401
from app.service import _review  # noqa: F401
from app.service import _test_runs  # noqa: F401
from app.service import _manage  # noqa: F401
from app.service._base import (  # noqa: F401
    SCHEMA, ProgressEvent, ProgressFn, Cancelled, CancelToken, _noop,
)
from app.service._erase_compose import (  # noqa: F401
    EraseResult, ComposeResult, _output_dirs, _commit, _discard, _included, _boxes, erase, _erase, _erase_chunk,
    _erase_part, _erase_parallel, _stream_meta, _verify, _finish_erase, export_excel, _reuse_dir, compose,
)
from app.service._extract import (  # noqa: F401
    ExtractResult, EXTRACT_MODES, extract, learn_tag_colors, evaluate_extract, list_games, new_project, list_groups,
    back_to_extracting, add_files, ProjectExtractResult, scoped_file_dirs, extract_project, IconScanResult,
    ICON_SUGGEST_MODEL, find_icons, IconImportResult, import_icons,
)
from app.service._glossary import (  # noqa: F401
    GlossaryReviewResult, GLOSSARY_MODEL, finish_extraction, GlossaryConfirmResult, import_glossary,
    glossary_review_rows, save_glossary_rows, confirm_glossary_rows, _confirm_review, _changed_terms, _TERM_JOSA,
    _JOSA_PAIRS, _is_hangul, _fit_josa, _swap_text, _term_source_counts, _term_plan, _swap_terms, _term_cells,
    term_change_candidates, TermApplyResult, apply_term_changes,
)
from app.service._translate_erase import (  # noqa: F401
    TRANSLATE_MODELS, translate_project, game_dir, review_xlsx_for, export_project_excel, _retire_legacy,
    cards_from_translation, ProjectStepResult, erase_project, erase_sig, KEEP_ORIGINAL, erase_sources,
    icon_only_regions, set_keep_original, stale_erased_pages, files_needing_erase, _cleared_pages, set_card_limit,
    compose_project, image_sheet_layouts, sheet_family, _attach_sheet_images, EDITED_DIR, EDITED_PDF,
    EditedImportResult, can_import_edited, edited_sheet_images, import_edited_pdf, _compose_with_cards,
)
from app.service._overview import (  # noqa: F401
    REGION_KINDS, ERASE_MODES, ALIGNS, list_projects, STATUS_LABEL, project_overview, _TRANSLATED_CACHE,
    _translated_files, _next_key, _next_step, set_pages, move_pages_to_group, page_count, shown_pages, page_image,
    pdf_page_image,
)
from app.service._fonts import (  # noqa: F401
    font_choices, font_pack_status, FONT_PICK_MODEL, font_plans, save_font_choices, font_preview, font_label,
    font_path, _INSTALLED_FONTS, installed_fonts, font_cache_dir, prepare_font, USER_FONT_DIR, FONT_EXTS,
    user_font_dir, user_fonts, font_file_info,
)
from app.service._regions import (  # noqa: F401
    load_region_template, validate_region_template, _validate_region_set, save_region_template, AlignResult,
    _group_files, _align_reference, align_group, align_info, nudge_alignment, reset_alignment, align_rect,
)
from app.service._settings import (  # noqa: F401
    engine_status, get_settings, save_settings, DONE_SOUND_MIN_S, save_done_sound, done_sound_file,
    default_done_sound, data_dir,
)
from app.service._cells_icons import (  # noqa: F401
    extracted_cells, icon_reread_pending, skip_icon_reread, known_icon_names, region_crop, save_source, RereadResult, reread_cells, reread_region,
    icon_review, icon_place, set_icon_box, absorb_icon_run, overlapping_icons, split_icon_box, set_icon_not_icon, restore_icon, reopen_icon, icon_box_problem, keep_cell_original, cell_kept,
    suggest_icon_name, icon_dictionary,
    update_icon_dictionary, confirm_icons,
)
from app.service._review import (  # noqa: F401
    result_pages, card_erase_info, set_card_erase_mode, card_text_offset, card_text_offsets, set_card_text_offset,
    card_edit_info, split_paragraphs, _TRANSLATION_CARDS,
    _NOT_FOR_CARDS, _file_json_sig, _regions_sig, _translation_cards, _translation_cards_read, save_card_edit,
    card_preview, REVIEW_STATUS, _review_check, _review_file_rows, _review_counts, _excel_info, review_rows,
    _review_row, check_review_text, save_review, set_review_checked, resolve_review_conflict, _REVIEW_PROGRESS,
    _clear_review_caches, review_progress, recompose_file, redo_file,
)
from app.service._test_runs import (  # noqa: F401
    TestRunsResult, test_run_cards, test_run_selection, TestRunResult, test_run, TEST_HISTORY_FILE,
    TEST_HISTORY_KEEP, ALIGN_CHECK, _ICON_TOKEN, _FORMAT_TAG, _source_counts, pick_hard_cards, parse_test_pages,
    _test_group_dir, _test_file_dir, _align_checks, _test_summary, _test_runs_root, _remove_test_dir,
    _read_test_history, _record_test, test_history, _test_entry, rerun_test, _test_cards, compare_tests,
    remember_project, last_project,
)
from app.service._manage import (  # noqa: F401
    self_check, license_text, release_notes, _to_trash, delete_project, delete_game, RESET_WHAT, reset_files,
    rename_group, delete_group, delete_file, api_key_info, set_api_key, clear_api_key, claude_login_status,
    launch_claude_login, install_claude_code, start_logging, log_folder, log_job_time, timing_report, export_logs,
    set_engine, ImageAddResult, image_preview, image_display, card_reading_order, pdf_page_count, pdf_sheet_images,
    add_images,
)


class _ServiceModule(types.ModuleType):
    """`service.이름 = 값`(시험의 monkeypatch 등)을 그 이름을 가진 하위 모듈에도 똑같이 넣는다.
    하위 모듈은 서로 `모듈.이름`이나 자기 이름으로 부르므로, 이렇게 해야 바꿔 끼운 것이 어디서 부르든 쓰인다."""

    def __setattr__(self, name, value):
        old = self.__dict__.get(name, _MISSING)
        if old is not _MISSING and not isinstance(old, types.ModuleType):
            for mod in _SUBMODULES:
                if mod.__dict__.get(name, _MISSING) is old:
                    setattr(mod, name, value)
        super().__setattr__(name, value)


_MISSING = object()
_SUBMODULES = (_base, _erase_compose, _extract, _glossary, _translate_erase, _overview, _fonts, _regions, _settings,
               _cells_icons, _review, _test_runs, _manage)
sys.modules[__name__].__class__ = _ServiceModule
