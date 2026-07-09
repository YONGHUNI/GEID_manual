# GEID Python Automation Manual

GEID Python 자동화 매뉴얼을 Quarto book 형식으로 정리한 저장소입니다.

이 문서는 Google Earth Images Downloader(GEID)의 `downloader.exe`를 Python에서 반복 호출하고, 다운로드된 JPG/JGW patch를 실제 촬영일 기준으로 분류한 뒤, 날짜별 GeoTIFF로 병합하는 흐름을 설명합니다.

## 문서 보기

로컬에서 렌더된 HTML은 다음 위치에 생성됩니다.

```text
_book/index.html
```

Quarto가 PATH에 잡혀 있다면 다음처럼 렌더합니다.

```powershell
quarto render
```

PATH에 잡혀 있지 않다면 Windows에서 다음처럼 실행할 수 있습니다.

```powershell
& "C:\Program Files\Quarto\bin\quarto.exe" render
```

## 실행 환경

Python 환경은 `environment.yml`을 기준으로 합니다.

기존 `geid` mamba 환경을 갱신할 때:

```powershell
mamba env update -n geid -f environment.yml
mamba activate geid
```

새 환경을 만들 때:

```powershell
mamba env create -f environment.yml
mamba activate geid
```

GEID 자체는 Python 패키지가 아니므로 별도로 설치해야 합니다.

- GEID 공식 페이지: <https://www.allmapsoft.com/geid/index.html>
- GEID 다운로드 페이지: <https://www.allmapsoft.com/geid/download.html>

설치 후 `downloader.exe` 경로를 `main.py` 또는 `GEIDPipelineManager(geid_exe_path=...)`에 넘깁니다.

## GEID 초기 설정

GEID GUI를 한 번 실행한 뒤 Options에서 다음 옵션을 켜야 합니다.

```text
Create .jgw file for each jpg tile
```

`.jgw` world file은 JPG tile의 공간 위치를 계산하는 데 필요합니다. 이 옵션이 꺼져 있으면 날짜별 merge가 정상적으로 동작하지 않을 수 있습니다.

또한 GEID trial version은 high zoom level 다운로드가 제한되며, 공식 페이지 기준 최대 zoom level은 13입니다. ZL 13보다 높은 고해상도 이미지를 받으려면 라이선스 구매 여부를 확인해야 합니다.

## 프로젝트 구조

```text
geid_manual/
├── environment.yml
├── _quarto.yml
├── index.qmd
├── 01-overview.qmd
├── ...
├── example_project/
├── output/
└── _book/
```

`example_project/`는 매뉴얼 안에서 실행 가능한 미니멀 예제입니다.

`output/`과 `example_project/output/`에는 문서의 동적 지도와 예제 결과에 연결된 산출물이 들어 있습니다.

## GitHub Pages 배포

Quarto의 GitHub Pages 배포 기능을 사용할 수 있습니다.

```powershell
quarto publish gh-pages
```

일반적인 repository site는 다음 주소 형식으로 접근합니다.

```text
https://<github-user>.github.io/<repo>/
```

렌더 산출물인 `_book/`을 main branch에 직접 커밋할지, `gh-pages` branch로만 배포할지는 저장소 운영 방식에 맞게 정하면 됩니다.

## 핵심 책임 분리

`main.py`는 실행 정책을 담당합니다.

- 입력 파일 또는 bbox 읽기
- `target_dates` 생성
- site별 반복 실행
- 날짜별 merge 트리거

`geid_pipeline/core.py`의 `GEIDPipelineManager`는 재사용 가능한 실행 엔진입니다.

- ROI grid 분할
- `downloader.exe` 실행
- GEID 로그 파싱
- 실제 촬영일별 patch 분류
- patch merge와 GeoTIFF 저장 함수 제공

`core.py`는 GeoPackage, Shapefile, CSV 같은 파일 형식을 직접 알지 않습니다. 파일을 읽고 `roi_gdf`, `roi_name`, `output_sub_dir`, `target_dates`, `zoom`으로 변환하는 일은 `main.py`가 담당합니다.
