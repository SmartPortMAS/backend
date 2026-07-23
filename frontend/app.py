import streamlit as st
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import time

st.set_page_config(page_title="액체화물 하역 관제 대시보드", page_icon="🚢", layout="wide")

# API URL
API_BASE = "http://backend:8000/api"

def fetch_data(endpoint):
    try:
        res = requests.get(f"{API_BASE}/{endpoint}")
        if res.status_code == 200:
            return res.json()
    except Exception:
        return None
    return None

# 상단 헤더
st.title("🚢 멀티 에이전트 기반 액체화물 하역 관제 시스템")
st.markdown("울산항 공공 API 및 AI 에이전트(기상/안전/스케줄링) 융합 실시간 관제 대시보드")

# 갱신 버튼
if st.sidebar.button("🔄 실시간 데이터 갱신"):
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("대시보드 메뉴")
menu = st.sidebar.radio("화면 선택", ["관제 메인보드", "선박 입출항 스케줄", "안전 관제 (혼재 위험)", "시뮬레이션 로그"])

# 데이터 로딩
with st.spinner("데이터 로딩 중..."):
    dashboard_data = fetch_data("dashboard")

if not dashboard_data:
    st.error("백엔드 서버에 연결할 수 없습니다. Docker Compose 환경에서 `backend` 서비스가 실행 중인지 확인하세요.")
    st.stop()

# 공통: 기상 정보 상단 표시
weather = dashboard_data.get("weather", {})
w_cols = st.columns(4)
w_cols[0].metric("기상 판정", weather.get("status", "-"), delta_color="inverse")
w_cols[1].metric("풍속 (m/s)", weather.get("wind_speed", "-"))
w_cols[2].metric("파고 (m)", weather.get("wave_height", "-"))
w_cols[3].metric("시정 (km)", weather.get("visibility", "-"))

st.markdown("---")

if menu == "관제 메인보드":
    st.subheader("📊 선석 점유 현황 및 탱크 여유 용량")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        berths = dashboard_data.get("berths", [])
        if berths:
            df_berths = pd.DataFrame(berths)
            
            # Gantt 차트를 위해 Plotly Timeline 사용
            df_gantt = []
            for b in berths:
                status = b['operation_status']
                color = "green" if status == "가용" else ("red" if status == "하역 중" else "orange")
                df_gantt.append({
                    "Berth": b['berth_name'],
                    "Status": status,
                    "Vessel": b['current_vessel'] if b['current_vessel'] else "비어있음",
                    "Cargo": b['current_cargo'] if b['current_cargo'] else "-"
                })
            
            df_display = pd.DataFrame(df_gantt)
            st.dataframe(df_display, use_container_width=True, hide_index=True)
            
    with col2:
        tanks = dashboard_data.get("tanks", [])
        if tanks:
            df_tanks = pd.DataFrame(tanks)
            # 탱크 재고율 게이지 차트 대신 간단한 바 차트
            fig = px.bar(df_tanks, x="terminal_name", y="stock_ratio_pct", color="cargo_name", 
                         title="터미널별 화물 재고율 (%)", text_auto=True)
            fig.update_layout(yaxis=dict(range=[0, 100]))
            st.plotly_chart(fig, use_container_width=True)

elif menu == "선박 입출항 스케줄":
    st.subheader("⛴️ 선박 입출항 예정 정보 (ETA/ETB)")
    vessels = dashboard_data.get("vessels", [])
    if vessels:
        df_v = pd.DataFrame(vessels)
        df_v['eta'] = pd.to_datetime(df_v['eta']).dt.strftime('%Y-%m-%d %H:%M')
        st.dataframe(df_v[['vessel_name', 'cargo', 'tonnage', 'assigned_berth', 'eta', 'status', 'dwt']], 
                     use_container_width=True, hide_index=True)
        
        # 에이전트 오케스트레이션 테스트 영역
        st.markdown("### 🤖 스케줄링 에이전트 승인 테스트")
        test_vessel = st.selectbox("테스트할 선박 선택", df_v['vessel_name'].tolist())
        sel_row = df_v[df_v['vessel_name'] == test_vessel].iloc[0]
        
        if st.button("에이전트 판단 요청"):
            with st.spinner("AI 오케스트레이션 실행 중..."):
                res = requests.get(f"{API_BASE}/agent/orchestrate", params={
                    "vessel_name": sel_row['vessel_name'],
                    "cargo_name": sel_row['cargo'],
                    "berth_name": sel_row['assigned_berth'],
                    "wind_speed": weather.get("wind_speed", 5.0),
                    "wave_height": weather.get("wave_height", 0.5)
                })
                if res.status_code == 200:
                    st.json(res.json())

elif menu == "안전 관제 (혼재 위험)":
    st.subheader("⚠️ 인접 선석 화물 혼재 위험 (GraphRAG 시나리오)")
    scenarios = fetch_data("simulation/scenarios")
    if scenarios:
        for s in scenarios:
            with st.expander(f"[{s['risk_level']}] {s['target_berth']} ({s['incoming_cargo']}) vs {s['adjacent_berth']} ({s['adjacent_cargo']})"):
                st.write(f"- 입항 선박: {s['incoming_vessel']} (화물: {s['incoming_cargo']})")
                st.write(f"- 인접 선박: {s['adjacent_vessel']} (화물: {s['adjacent_cargo']})")
                if s['is_prohibited']:
                    st.error(f"판단 사유: {s['reason']}")
                else:
                    st.success(f"판단 사유: {s['reason']}")
                    
        st.markdown("### 📄 화물 MSDS 및 체크리스트 조회")
        cargo_input = st.text_input("화물명 입력 (예: 벤젠, 황산, 메탄올)", "벤젠")
        if st.button("MSDS 조회"):
            res = requests.get(f"{API_BASE}/msds/{cargo_input}")
            if res.status_code == 200:
                data = res.json()
                if data['found']:
                    msds = data['data']
                    st.write(f"**UN No:** {msds['un_no']} | **위험등급:** {msds['위험등급']}")
                    st.write(f"**긴급조치:** {msds['긴급조치']}")
                    st.write("**안전 체크리스트:**")
                    for item in msds['안전체크리스트']:
                        st.markdown(f"- [ ] {item}")
                else:
                    st.warning(data['message'])

elif menu == "시뮬레이션 로그":
    st.subheader("📋 하역 작업 지연 및 완료 로그")
    logs = fetch_data("simulation/logs")
    if logs:
        df_logs = pd.DataFrame(logs)
        df_logs['start_time'] = pd.to_datetime(df_logs['start_time']).dt.strftime('%Y-%m-%d %H:%M')
        df_logs['expected_end_time'] = pd.to_datetime(df_logs['expected_end_time']).dt.strftime('%m-%d %H:%M')
        df_logs['actual_end_time'] = pd.to_datetime(df_logs['actual_end_time']).dt.strftime('%m-%d %H:%M')
        
        # 지연 경고 강조
        def highlight_delayed(row):
            if row['warning_issued']:
                return ['background-color: #ffcccc'] * len(row)
            return [''] * len(row)
            
        st.dataframe(df_logs.style.apply(highlight_delayed, axis=1), use_container_width=True, hide_index=True)
