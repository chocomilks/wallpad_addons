# Changelog

## 0.1.1
- 빌드 실패 수정: build.json 제거, Dockerfile에 베이스 이미지(python:3.13-slim-bookworm) 직접 지정

## 0.1.0
- 첫 버전: 도어카메라 캡처(SSH→망고→adb→cmxmm), SCRFD+ArcFace 인식, 사람별 정책(얼굴+폰 / 얼굴 단독 엄격),
  관리 패널(테스트·사람·등록 촬영·설정·기록), MQTT Discovery 엔티티, 문 열기 스크립트 호출
