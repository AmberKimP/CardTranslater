# CardTranslater — 작업 규칙

## 라이선스 (새 라이브러리·글꼴·그림)

이 프로그램은 Beerware(`LICENSE`, 저작자 AmberKimP)이고, 설치 파일은 PyMuPDF 때문에 전체가 AGPL-3.0 조건으로
배포된다 (`THIRD_PARTY_NOTICES.md`). 코드 리뷰(/code-review)와 모든 작업에서:

- **새 라이브러리를 넣거나 버전을 올리면 라이선스를 확인한다.** `requirements.txt`, 새 `import`, `packaging/build.py`의
  번들 내용이 바뀌는 diff는 리뷰에서 라이선스를 함께 본다.
  - 괜찮음: MIT·BSD·Apache-2.0·PSF·ISC·Zlib·MIT-CMU (고지만 하면 됨), LGPL (별도 DLL·폴더로 배포할 때),
    GPL·AGPL (이미 AGPL이므로 가능하지만 리뷰에 적는다).
  - 넣지 않음(먼저 사용자에게 묻기): 상업적 사용 금지(NonCommercial·PolyForm 등), 라이선스 표기 없음, 독점·사용 제한 라이선스,
    AGPL과 함께 배포할 수 없는 라이선스.
  - 넣기로 하면: `packaging/license_check.py`의 `APPROVED`에 이름·라이선스 표기를 적고, 설치 파일에 들어가면
    `THIRD_PARTY_NOTICES.md` 표에 넣는다. 실행용이면 `packaging/build.py`의 `RUNTIME_ROOTS`에도 넣는다.
  - `tests/test_licenses.py`가 이것을 지킨다 (목록에 없는 런타임 라이브러리, 라이선스가 바뀐 업그레이드, 라이선스 표기 없는
    `pymupdf_layout`·`pymupdf4llm`을 앱이 import하면 실패). 확인: `.venv/Scripts/python packaging/license_check.py`
- **글꼴**은 SIL OFL 1.1 등 PDF에 넣어도 되는 것만 `fonts/catalog.json`에 넣고 `OFL.txt`를 함께 둔다.
  수정(가변 글꼴 자르기 등)할 때는 그 글꼴의 예약된 이름(Reserved Font Name)을 쓰지 않는다.
- **그림·아이콘(`assets/`)** 은 출처와 사용 허락을 확인한다. 실제 상품·상표가 보이는 그림은 넣지 않는다.
