"""MSDS 안전 챗봇 요청/응답 스키마."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.safety.schemas import SafetyAssessmentResult


class Intent(str, Enum):
    """질문 유형. 이 값에 따라 어떤 근거를 모을지가 결정된다(service.py 분기)."""

    INCOMPATIBILITY_CHECK = "incompatibility_check"  # 두 화물을 같이 둬도 되는가
    INCOMPATIBLE_LIST = "incompatible_list"  # 특정 화물과 혼재금지인 화물 목록
    CHEMICAL_INFO = "chemical_info"  # 특정 화물의 MSDS 정보
    SAFETY_GENERAL = "safety_general"  # 특정 물질에 매이지 않은 일반 안전 질문
    OUT_OF_SCOPE = "out_of_scope"  # 화학물질 안전과 무관한 질문


class MatchMethod(str, Enum):
    """물질명 → chem_id 해석 방법. 근거 신뢰도 계산에 쓰인다."""

    CAS = "cas"  # 질문에 CAS번호가 직접 등장
    EXACT_NAME = "exact_name"  # DB 등재명과 정확히 일치
    ALIAS = "alias"  # 별칭 사전 매칭 (메탄올 → 메틸 알코올 등)
    VECTOR = "vector"  # pgvector 유사도 매칭


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class QueryPlan(BaseModel):
    """1단계 LLM의 구조화 출력. 질문 분류 + 물질명 추출을 한 번의 호출로 처리한다.

    호출을 나누면 지연시간과 비용이 두 배가 되는데, 두 작업 모두 같은 문장 하나만
    보면 되므로 나눌 이유가 없다.
    """

    intent: Intent
    chemicals: list[str] = Field(
        default_factory=list,
        description="질문에 등장한 화학물질명 원문 그대로 (한글/영문/약칭/CAS번호 포함)",
    )
    reasoning: str = Field(description="분류 근거 한 문장")


class ChemicalMatch(BaseModel):
    query_name: str = Field(description="사용자가 입력한 원본 물질명")
    chem_id: str
    name_ko: str | None = None
    name_en: str | None = None
    cas_no: str | None = None
    method: MatchMethod
    score: float | None = Field(
        default=None, description="VECTOR 매칭일 때의 코사인 유사도(0~1). 그 외에는 None"
    )


class RetrievedChunk(BaseModel):
    chem_id: str
    chemical_name: str
    cas_no: str | None = None
    section_key: str
    section_label: str
    content: str
    score: float


class IncompatibleCategoryGroup(BaseModel):
    """혼재금지 카테고리 하나와, 그 카테고리에 속하는(IS_CLASSIFIED_AS) 등재 화물들.

    Neo4j에 Chemical→Chemical 직접 관계가 없고 IncompatibleMaterial 카테고리 노드를
    경유하는 구조라(data-pipeline/loaders/msds_neo4j_loader.py), 목록 질문의 답도
    "카테고리 → 그에 속한 화물" 2단 구조로 내려간다.
    """

    category: str
    chemicals: list[ChemicalMatch] = Field(
        default_factory=list, description="이 카테고리로 분류된 DB 등재 화물 (없을 수 있음)"
    )


class ChemicalProfile(BaseModel):
    """단일 화물의 그래프 요약. LLM 프롬프트와 API 응답에 함께 쓴다."""

    chem_id: str
    name_ko: str | None = None
    name_en: str | None = None
    cas_no: str | None = None
    un_no: str | None = None
    in_graph: bool = Field(
        description=(
            "Neo4j 지식그래프에 이 화물의 노드가 있는지. false면 MSDS 원문은 있어도 "
            "혼재금지 관계는 '없음'이 아니라 '판정 불가'로 다뤄야 한다."
        )
    )
    hazard_classes: list[str] = Field(default_factory=list)
    incompatible_categories: list[str] = Field(default_factory=list)
    classified_as: list[str] = Field(default_factory=list)
    imdg_classes: list[str] = Field(default_factory=list)

    # ── msds_chemical 정형 값 컬럼 (Alembic 0007) ────────────────────────────
    # 그래프가 아니라 PostgreSQL에서 온다. in_graph=False인 화물도 이 값들은
    # 채워지므로, 프롬프트에서 그래프 관계보다 먼저 실어야 한다.
    # 벡터 검색으로 근사하던 값을 결정적으로 답하기 위한 것 — 근거 블록에
    # 확정값이 있으면 LLM이 청크를 뒤질 이유가 없다.
    flash_point_text: str | None = None
    boiling_point_text: str | None = None
    vapor_pressure_text: str | None = None
    specific_gravity_text: str | None = None
    packing_group: str | None = None
    ems_fire: str | None = None
    ems_spill: str | None = None
    signal_word: str | None = None
    exposure_limit_kr: str | None = None


class GraphEvidence(BaseModel):
    """LLM에 넘긴 그래프 근거를 그대로 응답에도 노출한다 — 답변의 검증 가능성 확보."""

    profiles: list[ChemicalProfile] = Field(default_factory=list)
    incompatible_groups: list[IncompatibleCategoryGroup] = Field(default_factory=list)


class LLMAnswer(BaseModel):
    """최종 답변 LLM의 구조화 출력."""

    answer: str = Field(description="관제사에게 보여줄 한국어 답변 (마크다운 허용)")
    safety_actions: list[str] = Field(
        default_factory=list, description="제공된 MSDS 문구에 근거한 안전조치 제안 (없으면 빈 배열)"
    )
    data_insufficient: bool = Field(
        description="제공된 근거만으로 질문에 답할 수 없으면 true"
    )


class CargoHint(BaseModel):
    """호출 측이 이미 화물을 특정하고 있을 때 넘기는 힌트.

    선석 배정 화면처럼 화물 객체를 이미 쥐고 있는 UI에서만 채워진다. 자유 채팅에는
    없는 게 정상이고(사용자가 CAS번호를 알 리 없다), 그때는 LLM 플래너가 질문에서
    물질명을 추출한다 — 즉 이 필드는 기능 요건이 아니라 최적화다.

    chem_id를 우선한다. cas_no는 msds_chemical에서 nullable이라 CAS 없는 화물이
    적재되면 지목할 수단이 사라진다(현재 35종은 전부 보유).
    """

    chem_id: str | None = Field(default=None, max_length=20)
    cas_no: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _at_least_one(self) -> "CargoHint":
        if not self.chem_id and not self.cas_no:
            raise ValueError("cargo_hint는 chem_id 또는 cas_no 중 하나가 있어야 합니다.")
        return self


class ChatRequest(BaseModel):
    """`POST /api/v1/rag/query` 요청.

    top_k(근거 청크 수)는 **의도적으로 받지 않는다.** 그 값은 화면에 몇 개를 보여줄지가
    아니라 LLM 프롬프트에 들어가는 근거 수라, 클라이언트가 바꾸면 같은 질문에 다른
    답이 나온다 — "챗봇과 대시보드가 같은 질문에 다른 답을 내면 안 된다"는 원칙이
    API 파라미터 하나로 무너진다. 게다가 백엔드는 intent별로 다르게 튜닝해 두었고
    (일반 8 / 혼재판정 4 / 그 외 6), 단일 값을 받으면 그 분기가 무력화된다.
    근거를 더 보여주는 건 citations를 접었다 펴는 UI 문제지 재검색할 일이 아니다.

    extra="forbid" — 모르는 필드는 조용히 무시하지 않고 422로 거절한다. 무시하면
    클라이언트는 그 필드가 동작한다고 믿게 되고, 나중에 "답변이 부실하다"는 제보가
    와도 원인을 추적할 수 없다.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"question": "벤젠 취급 시 보호구는?"},
                {"question": "취급 시 보호구는?", "cargo_hint": {"chem_id": "001008"}},
                {"question": "벤젠과 가솔린을 인접 선석에 둬도 되나요?"},
            ]
        },
    )

    question: str = Field(
        min_length=1, max_length=1000,
        description="자연어 질문. 통칭도 인식합니다(휘발유·벙커C·PX·IPA 등).",
        examples=["벤젠 취급 시 보호구는?"],
    )
    cargo_hint: CargoHint | None = Field(
        default=None,
        description="화면이 이미 화물을 특정한 경우에만. 자유 채팅에서는 보내지 마세요.",
    )


class Citation(BaseModel):
    """답변 근거 1건. 청크·정형 컬럼·그래프 관계를 한 형태로 통합한다.

    호출 측에 내부 3계층 구조를 노출하지 않기 위한 표현이다 — 어느 계층에서 왔는지는
    백엔드 사정이고, 나중에 계층이 늘어도 이 스키마는 그대로다.

    score가 None이면 **확정값**(정형 컬럼 또는 그래프 관계)이라는 뜻이다. 유사도 개념이
    없고 오히려 벡터 검색 결과보다 신뢰도가 높으므로, 화면에서 0.82짜리 청크와 같아
    보이면 안 된다.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"chem_name": "벤젠", "cas_no": "71-43-2", "section": "detail08",
                 "section_name": "노출방지 및 개인보호구",
                 "text": "[벤젠 - 노출방지 및 개인보호구]\n국내규정: TWA : 0.5ppm …",
                 "score": 0.3859},
                {"chem_name": "벤젠", "cas_no": "71-43-2", "section": "detail09",
                 "section_name": "물리화학적 특성", "text": "인화점: -11 ℃", "score": None},
            ]
        }
    )

    chem_name: str = Field(description="화물 등재명", examples=["벤젠"])
    cas_no: str | None = Field(default=None, examples=["71-43-2"])
    section: str | None = Field(
        default=None, description="MSDS 섹션 키 (detail01~detail16)", examples=["detail08"]
    )
    section_name: str | None = Field(
        default=None, description="섹션 한글명", examples=["노출방지 및 개인보호구"]
    )
    text: str = Field(description="근거 원문. 청크는 MSDS 발췌, 확정값은 '항목명: 값' 형태")
    score: float | None = Field(
        default=None,
        description="벡터 유사도(0~1). **None이면 확정값**(MSDS 정형 항목 또는 지식그래프 "
        "관계에서 온 값)이며 유사도 검색 결과보다 신뢰도가 높다. 화면에서 구분해 표시할 것.",
        examples=[0.3859],
    )


class RagAssessment(BaseModel):
    """혼재 판정 결과 요약. 규칙엔진 하한과 IMDG 공인 격리표로 보정된 값이라
    답변 문장에 녹이지 않고 별도 필드로 노출한다 — 대시보드와 같은 등급을 써야 한다."""

    risk_level: str = Field(
        description="최종 위험등급. 이 값을 결론으로 표시할 것(답변 문장이 아니라).",
        examples=["안전", "주의", "위험", "배정불가"],
    )
    rule_engine_floor: str = Field(
        description="규칙엔진이 보장한 하한. LLM은 이보다 낮출 수 없다.", examples=["주의"]
    )
    reasoning: str = Field(description="판정 근거 요약")


class RagQueryResponse(BaseModel):
    """`POST /api/v1/rag/query` 응답.

    `answer` + `citations`가 계약의 본체이고, 나머지 세 필드는 문서 검색 계약으로는
    표현할 수 없는 **안전 도메인 사실**이라 추가로 싣는다. 클라이언트가 모르는 필드는
    무시하면 되므로 하위 호환이 깨지지 않는다.
      - confidence — LLM이 아니라 근거 종류로 코드가 산정한 값
      - unresolved — DB 미등재 물질. "관계 없음"이 아니라 "판정 불가"임을 구조적으로 알린다
      - assessment — 혼재 판정 질문일 때만
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "answer": "벤젠의 인화점은 -11 ℃입니다.",
                    "citations": [
                        {"chem_name": "벤젠", "cas_no": "71-43-2", "section": "detail09",
                         "section_name": "물리화학적 특성",
                         "text": "[벤젠 - 물리화학적 특성]\n성상: (무색 혹은 옅은…",
                         "score": 0.6461},
                        {"chem_name": "벤젠", "cas_no": "71-43-2", "section": "detail09",
                         "section_name": "물리화학적 특성",
                         "text": "인화점: -11 ℃", "score": None},
                    ],
                    "confidence": "medium",
                    "unresolved": [],
                    "assessment": None,
                },
                {
                    "answer": "질산암모늄은(는) 현재 DB에 등록되어 있지 않아 답변할 수 없습니다.",
                    "citations": [],
                    "confidence": "low",
                    "unresolved": ["질산암모늄"],
                    "assessment": None,
                },
            ]
        }
    )

    answer: str = Field(description="관제사에게 보여줄 답변 본문. 마크다운을 포함할 수 있다.")
    citations: list[Citation] = Field(
        default_factory=list,
        description="답변의 근거. `score`가 None이면 확정값, 숫자면 벡터 검색 결과.",
    )
    confidence: Confidence = Field(
        description="근거의 종류로 **코드가 산정한** 신뢰도(LLM에게 묻지 않는다). "
        "high=그래프 관계·안전판정으로 뒷받침 / medium=근거는 있으나 해석·등재가 불완전 / "
        "low=근거 부족, 원본 MSDS 재확인 필요",
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="DB 미등재 물질명. **비어 있지 않으면 반드시 경고를 띄울 것** — "
        "'혼재금지 관계가 없다'가 아니라 '판정할 수 없다'는 뜻이다.",
        examples=[["질산암모늄"]],
    )
    assessment: RagAssessment | None = Field(
        default=None, description="혼재 판정 질문일 때만 채워진다."
    )


class ChatResponse(BaseModel):
    answer: str
    intent: Intent
    confidence: Confidence
    chemicals_resolved: list[ChemicalMatch] = Field(default_factory=list)
    chemicals_unresolved: list[str] = Field(
        default_factory=list, description="DB 34종에 없어 해석하지 못한 물질명"
    )
    safety_actions: list[str] = Field(default_factory=list)
    graph_evidence: GraphEvidence = Field(default_factory=GraphEvidence)
    safety_assessment: SafetyAssessmentResult | None = Field(
        default=None,
        description="혼재 판정 질문일 때 안전관제 에이전트(app/agents/safety)의 판정 결과 원문",
    )
    retrieved_chunks: list[RetrievedChunk] = Field(
        default_factory=list, description="LLM 근거로 사용한 MSDS 벡터 검색 결과"
    )
    sources: list[str] = Field(
        default_factory=list, description="근거 출처 표기용 (예: 'MSDS 벤젠 / CAS 71-43-2')"
    )


class ChemicalListItem(BaseModel):
    chem_id: str
    name_ko: str | None = None
    name_en: str | None = None
    cas_no: str | None = None
    un_no: str | None = None
