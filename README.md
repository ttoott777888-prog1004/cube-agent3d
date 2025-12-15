# cube-agent3d (v0.1.0)

로컬에서 실행되는 3D 씬(Three.js) + Python 에이전트 엔진입니다.  
브라우저 버튼으로 AI를 시작/정지하면, 에이전트가 큐브를 복제/이동/회전/스케일/색상 변경하며 행동을 로그로 저장합니다.

## 설치(개발 설치)
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

pip install -U pip
pip install -e .
```

## 실행
```bash
cube-agent3d run --host 127.0.0.1 --port 8000
```

브라우저: http://127.0.0.1:8000

## 로그
실행할 때마다 `sessions/<session_id>/` 생성:
- `actions.jsonl` : tick별 액션 배치
- `summary.jsonl` : tick별 점수/큐브수/요약
- `snapshots.jsonl` : tick별 상태 스냅샷(기본: 매 tick)

## 유지보수/피드백
이 프로젝트는 지원/업데이트를 보장하지 않습니다.
