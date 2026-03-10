--
-- PostgreSQL database dump
--

\restrict xcAfotmxvgWsv0A38cQfpsFLVdUaROEjhT8gIh3jM3falDQEXaV86SzNBnRmLLf

-- Dumped from database version 16.13 (Debian 16.13-1.pgdg13+1)
-- Dumped by pg_dump version 16.13 (Debian 16.13-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: actions; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.actions (
    id integer NOT NULL,
    goal_id integer NOT NULL,
    engine character varying(120) NOT NULL,
    action_type character varying(120) NOT NULL,
    input_ref text NOT NULL,
    expected_state_delta json NOT NULL,
    validation_method character varying(120) NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    status character varying(40) NOT NULL,
    sequence_index integer DEFAULT 1,
    depends_on_action_id integer,
    parent_action_id integer,
    retry_of_action_id integer,
    retry_count integer DEFAULT 0,
    replaced_action_id integer,
    replacement_action_id integer,
    recovery_classification character varying(40) DEFAULT ''::character varying,
    chain_event character varying(40) DEFAULT ''::character varying
);


ALTER TABLE public.actions OWNER TO mim_prod;

--
-- Name: actions_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.actions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.actions_id_seq OWNER TO mim_prod;

--
-- Name: actions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.actions_id_seq OWNED BY public.actions.id;


--
-- Name: actors; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.actors (
    id integer NOT NULL,
    name character varying(120) NOT NULL,
    role character varying(80) NOT NULL,
    identity_metadata json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.actors OWNER TO mim_prod;

--
-- Name: actors_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.actors_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.actors_id_seq OWNER TO mim_prod;

--
-- Name: actors_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.actors_id_seq OWNED BY public.actors.id;


--
-- Name: execution_journal; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.execution_journal (
    id integer NOT NULL,
    actor character varying(120) NOT NULL,
    action character varying(200) NOT NULL,
    target_type character varying(80) NOT NULL,
    target_id character varying(120) NOT NULL,
    idempotency_key character varying(120),
    result text NOT NULL,
    metadata_json json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.execution_journal OWNER TO mim_prod;

--
-- Name: execution_journal_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.execution_journal_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.execution_journal_id_seq OWNER TO mim_prod;

--
-- Name: execution_journal_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.execution_journal_id_seq OWNED BY public.execution_journal.id;


--
-- Name: goal_plans; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.goal_plans (
    id integer NOT NULL,
    goal_id integer NOT NULL,
    ordered_action_ids json NOT NULL,
    current_step_index integer NOT NULL,
    derived_status character varying(40) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.goal_plans OWNER TO mim_prod;

--
-- Name: goal_plans_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.goal_plans_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.goal_plans_id_seq OWNER TO mim_prod;

--
-- Name: goal_plans_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.goal_plans_id_seq OWNED BY public.goal_plans.id;


--
-- Name: goals; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.goals (
    id integer NOT NULL,
    objective_id integer,
    task_id integer,
    goal_type character varying(80) NOT NULL,
    goal_description text NOT NULL,
    requested_by character varying(120) NOT NULL,
    priority character varying(40) NOT NULL,
    status character varying(40) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.goals OWNER TO mim_prod;

--
-- Name: goals_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.goals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.goals_id_seq OWNER TO mim_prod;

--
-- Name: goals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.goals_id_seq OWNED BY public.goals.id;


--
-- Name: memory_entries; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.memory_entries (
    id integer NOT NULL,
    memory_class character varying(60) NOT NULL,
    content text NOT NULL,
    summary text NOT NULL,
    metadata_json json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.memory_entries OWNER TO mim_prod;

--
-- Name: memory_entries_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.memory_entries_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.memory_entries_id_seq OWNER TO mim_prod;

--
-- Name: memory_entries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.memory_entries_id_seq OWNED BY public.memory_entries.id;


--
-- Name: memory_links; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.memory_links (
    id integer NOT NULL,
    source_memory_id integer NOT NULL,
    target_memory_id integer NOT NULL,
    relation character varying(80) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.memory_links OWNER TO mim_prod;

--
-- Name: memory_links_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.memory_links_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.memory_links_id_seq OWNER TO mim_prod;

--
-- Name: memory_links_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.memory_links_id_seq OWNED BY public.memory_links.id;


--
-- Name: objectives; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.objectives (
    id integer NOT NULL,
    title character varying(200) NOT NULL,
    description text NOT NULL,
    priority character varying(40) NOT NULL,
    constraints_json json NOT NULL,
    success_criteria text NOT NULL,
    state character varying(40) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.objectives OWNER TO mim_prod;

--
-- Name: objectives_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.objectives_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.objectives_id_seq OWNER TO mim_prod;

--
-- Name: objectives_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.objectives_id_seq OWNED BY public.objectives.id;


--
-- Name: projects; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.projects (
    id integer NOT NULL,
    name character varying(200) NOT NULL,
    description text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.projects OWNER TO mim_prod;

--
-- Name: projects_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.projects_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.projects_id_seq OWNER TO mim_prod;

--
-- Name: projects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;


--
-- Name: routing_engine_summaries; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.routing_engine_summaries (
    id integer NOT NULL,
    engine_name character varying(120) NOT NULL,
    runs integer NOT NULL,
    pass_rate double precision NOT NULL,
    review_correction_rate double precision NOT NULL,
    blocked_rate double precision NOT NULL,
    avg_latency_ms double precision NOT NULL,
    fallback_rate double precision NOT NULL,
    weighted_recent_score double precision NOT NULL,
    sample_window integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.routing_engine_summaries OWNER TO mim_prod;

--
-- Name: routing_engine_summaries_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.routing_engine_summaries_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.routing_engine_summaries_id_seq OWNER TO mim_prod;

--
-- Name: routing_engine_summaries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.routing_engine_summaries_id_seq OWNED BY public.routing_engine_summaries.id;


--
-- Name: routing_execution_metrics; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.routing_execution_metrics (
    id integer NOT NULL,
    task_id integer,
    objective_id integer,
    selected_engine character varying(120) NOT NULL,
    fallback_engine character varying(120) NOT NULL,
    fallback_used boolean NOT NULL,
    routing_source character varying(120) NOT NULL,
    routing_confidence double precision NOT NULL,
    policy_version character varying(80) NOT NULL,
    engine_version character varying(120) NOT NULL,
    routing_selection_reason text NOT NULL,
    routing_final_outcome character varying(40) NOT NULL,
    latency_ms integer NOT NULL,
    result_category character varying(80) NOT NULL,
    failure_category character varying(120) NOT NULL,
    review_outcome character varying(40) NOT NULL,
    blocked_pre_invocation boolean NOT NULL,
    metadata_json json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.routing_execution_metrics OWNER TO mim_prod;

--
-- Name: routing_execution_metrics_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.routing_execution_metrics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.routing_execution_metrics_id_seq OWNER TO mim_prod;

--
-- Name: routing_execution_metrics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.routing_execution_metrics_id_seq OWNED BY public.routing_execution_metrics.id;


--
-- Name: services; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.services (
    id integer NOT NULL,
    name character varying(120) NOT NULL,
    status character varying(40) NOT NULL,
    heartbeat_at timestamp with time zone,
    dependency_map json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.services OWNER TO mim_prod;

--
-- Name: services_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.services_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.services_id_seq OWNER TO mim_prod;

--
-- Name: services_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.services_id_seq OWNED BY public.services.id;


--
-- Name: state_snapshots; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.state_snapshots (
    id integer NOT NULL,
    goal_id integer NOT NULL,
    action_id integer NOT NULL,
    snapshot_phase character varying(20) NOT NULL,
    state_type character varying(80) NOT NULL,
    state_payload json NOT NULL,
    captured_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.state_snapshots OWNER TO mim_prod;

--
-- Name: state_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.state_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.state_snapshots_id_seq OWNER TO mim_prod;

--
-- Name: state_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.state_snapshots_id_seq OWNED BY public.state_snapshots.id;


--
-- Name: task_results; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.task_results (
    id integer NOT NULL,
    task_id integer NOT NULL,
    result text NOT NULL,
    files_changed json NOT NULL,
    tests_run json NOT NULL,
    test_results text NOT NULL,
    failures json NOT NULL,
    recommendations text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.task_results OWNER TO mim_prod;

--
-- Name: task_results_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.task_results_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.task_results_id_seq OWNER TO mim_prod;

--
-- Name: task_results_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.task_results_id_seq OWNED BY public.task_results.id;


--
-- Name: task_reviews; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.task_reviews (
    id integer NOT NULL,
    task_id integer NOT NULL,
    reviewer character varying(120) NOT NULL,
    status character varying(50) NOT NULL,
    notes text NOT NULL,
    continue_allowed boolean NOT NULL,
    escalate_to_user boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.task_reviews OWNER TO mim_prod;

--
-- Name: task_reviews_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.task_reviews_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.task_reviews_id_seq OWNER TO mim_prod;

--
-- Name: task_reviews_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.task_reviews_id_seq OWNED BY public.task_reviews.id;


--
-- Name: tasks; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.tasks (
    id integer NOT NULL,
    objective_id integer,
    title character varying(200) NOT NULL,
    details text NOT NULL,
    dependencies json NOT NULL,
    acceptance_criteria text NOT NULL,
    assigned_to character varying(120) NOT NULL,
    state character varying(40) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.tasks OWNER TO mim_prod;

--
-- Name: tasks_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.tasks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tasks_id_seq OWNER TO mim_prod;

--
-- Name: tasks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.tasks_id_seq OWNED BY public.tasks.id;


--
-- Name: tool_invocations; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.tool_invocations (
    id integer NOT NULL,
    tool_id integer NOT NULL,
    actor character varying(120) NOT NULL,
    input_json json NOT NULL,
    output_json json NOT NULL,
    status character varying(40) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.tool_invocations OWNER TO mim_prod;

--
-- Name: tool_invocations_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.tool_invocations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tool_invocations_id_seq OWNER TO mim_prod;

--
-- Name: tool_invocations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.tool_invocations_id_seq OWNED BY public.tool_invocations.id;


--
-- Name: tools; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.tools (
    id integer NOT NULL,
    name character varying(120) NOT NULL,
    description text NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.tools OWNER TO mim_prod;

--
-- Name: tools_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.tools_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.tools_id_seq OWNER TO mim_prod;

--
-- Name: tools_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.tools_id_seq OWNED BY public.tools.id;


--
-- Name: validation_results; Type: TABLE; Schema: public; Owner: mim_prod
--

CREATE TABLE public.validation_results (
    id integer NOT NULL,
    goal_id integer NOT NULL,
    action_id integer NOT NULL,
    validation_method character varying(120) NOT NULL,
    validation_status character varying(40) NOT NULL,
    validation_details json NOT NULL,
    validated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.validation_results OWNER TO mim_prod;

--
-- Name: validation_results_id_seq; Type: SEQUENCE; Schema: public; Owner: mim_prod
--

CREATE SEQUENCE public.validation_results_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.validation_results_id_seq OWNER TO mim_prod;

--
-- Name: validation_results_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: mim_prod
--

ALTER SEQUENCE public.validation_results_id_seq OWNED BY public.validation_results.id;


--
-- Name: actions id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.actions ALTER COLUMN id SET DEFAULT nextval('public.actions_id_seq'::regclass);


--
-- Name: actors id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.actors ALTER COLUMN id SET DEFAULT nextval('public.actors_id_seq'::regclass);


--
-- Name: execution_journal id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.execution_journal ALTER COLUMN id SET DEFAULT nextval('public.execution_journal_id_seq'::regclass);


--
-- Name: goal_plans id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.goal_plans ALTER COLUMN id SET DEFAULT nextval('public.goal_plans_id_seq'::regclass);


--
-- Name: goals id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.goals ALTER COLUMN id SET DEFAULT nextval('public.goals_id_seq'::regclass);


--
-- Name: memory_entries id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.memory_entries ALTER COLUMN id SET DEFAULT nextval('public.memory_entries_id_seq'::regclass);


--
-- Name: memory_links id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.memory_links ALTER COLUMN id SET DEFAULT nextval('public.memory_links_id_seq'::regclass);


--
-- Name: objectives id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.objectives ALTER COLUMN id SET DEFAULT nextval('public.objectives_id_seq'::regclass);


--
-- Name: projects id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);


--
-- Name: routing_engine_summaries id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.routing_engine_summaries ALTER COLUMN id SET DEFAULT nextval('public.routing_engine_summaries_id_seq'::regclass);


--
-- Name: routing_execution_metrics id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.routing_execution_metrics ALTER COLUMN id SET DEFAULT nextval('public.routing_execution_metrics_id_seq'::regclass);


--
-- Name: services id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.services ALTER COLUMN id SET DEFAULT nextval('public.services_id_seq'::regclass);


--
-- Name: state_snapshots id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.state_snapshots ALTER COLUMN id SET DEFAULT nextval('public.state_snapshots_id_seq'::regclass);


--
-- Name: task_results id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.task_results ALTER COLUMN id SET DEFAULT nextval('public.task_results_id_seq'::regclass);


--
-- Name: task_reviews id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.task_reviews ALTER COLUMN id SET DEFAULT nextval('public.task_reviews_id_seq'::regclass);


--
-- Name: tasks id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tasks ALTER COLUMN id SET DEFAULT nextval('public.tasks_id_seq'::regclass);


--
-- Name: tool_invocations id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tool_invocations ALTER COLUMN id SET DEFAULT nextval('public.tool_invocations_id_seq'::regclass);


--
-- Name: tools id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tools ALTER COLUMN id SET DEFAULT nextval('public.tools_id_seq'::regclass);


--
-- Name: validation_results id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.validation_results ALTER COLUMN id SET DEFAULT nextval('public.validation_results_id_seq'::regclass);


--
-- Data for Name: actions; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.actions (id, goal_id, engine, action_type, input_ref, expected_state_delta, validation_method, started_at, completed_at, status, sequence_index, depends_on_action_id, parent_action_id, retry_of_action_id, retry_count, replaced_action_id, replacement_action_id, recovery_classification, chain_event) FROM stdin;
1	2	local	prod-probe	prod://objective18	{"counter": 1}	expected_delta_compare	2026-03-10 05:09:36.228338+00	2026-03-10 05:09:36.22834+00	completed	1	\N	\N	\N	0	\N	\N		
2	4	local	prod-custody-probe	prod://o181	{"counter": 1}	expected_delta_compare	2026-03-10 05:27:02.251289+00	2026-03-10 05:27:02.251292+00	completed	1	\N	\N	\N	0	\N	\N		
3	5	local	probe_step_1	prod://probe/1	{"counter": 1}	expected_delta_compare	2026-03-10 05:57:07.690659+00	2026-03-10 05:57:07.690662+00	completed	1	\N	\N	\N	0	\N	\N		
4	5	local	probe_step_2	prod://probe/2	{"counter": 1}	expected_delta_compare	2026-03-10 05:57:07.745937+00	2026-03-10 05:57:07.745941+00	failed	2	3	3	\N	0	\N	\N		
5	5	local	probe_step_3	prod://probe/3	{"counter": 1}	expected_delta_compare	2026-03-10 05:57:07.785217+00	2026-03-10 05:57:07.78522+00	skipped	3	4	3	\N	0	\N	\N		
6	6	engine-a	step-1	prod://o20/s1	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:30.11612+00	2026-03-10 06:09:30.116123+00	completed	1	\N	\N	\N	0	\N	\N		
9	6	engine-b	step-2-retry	prod://o20/s2/retry	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:30.194474+00	2026-03-10 06:09:30.194477+00	completed	4	6	6	7	1	\N	\N	recovered	retry
8	6	engine-a	step-3	prod://o20/s3	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:30.17122+00	2026-03-10 06:09:30.216118+00	skipped	3	7	\N	\N	0	\N	\N		skip
10	6	engine-c	step-2-replacement	prod://o20/s2/replace	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:30.23929+00	2026-03-10 06:09:30.239292+00	completed	5	6	6	\N	0	7	\N	recovered	replace
7	6	engine-a	step-2	prod://o20/s2	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:30.148823+00	2026-03-10 06:09:30.148825+00	failed	2	6	6	\N	0	\N	10		
11	6	system	resume_chain	resume://goal	{}	manual_resume	2026-03-10 06:09:30.261678+00	2026-03-10 06:09:30.26168+00	completed	6	10	\N	\N	0	\N	\N	recovered	resume
12	7	engine-a	step-1	prod://o20/s1	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:50.155307+00	2026-03-10 06:09:50.155311+00	completed	1	\N	\N	\N	0	\N	\N		
15	7	engine-b	step-2-retry	prod://o20/s2/retry	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:50.235206+00	2026-03-10 06:09:50.235208+00	completed	4	12	12	13	1	\N	\N	recovered	retry
14	7	engine-a	step-3	prod://o20/s3	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:50.211364+00	2026-03-10 06:09:50.256696+00	skipped	3	13	\N	\N	0	\N	\N		skip
16	7	engine-c	step-2-replacement	prod://o20/s2/replace	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:50.279185+00	2026-03-10 06:09:50.279188+00	completed	5	12	12	\N	0	13	\N	recovered	replace
13	7	engine-a	step-2	prod://o20/s2	{"counter": 1}	expected_delta_compare	2026-03-10 06:09:50.188794+00	2026-03-10 06:09:50.188797+00	failed	2	12	12	\N	0	\N	16		
17	7	system	resume_chain	resume://goal	{}	manual_resume	2026-03-10 06:09:50.302196+00	2026-03-10 06:09:50.302199+00	completed	6	16	\N	\N	0	\N	\N	recovered	resume
\.


--
-- Data for Name: actors; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.actors (id, name, role, identity_metadata, created_at) FROM stdin;
\.


--
-- Data for Name: execution_journal; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.execution_journal (id, actor, action, target_type, target_id, idempotency_key, result, metadata_json, created_at) FROM stdin;
1	ops	objective17_promoted	objective	17	objective17-promote-20260310	Objective 17 routing learning promoted to production after PASS gate and post-verification	{"release_tag": "objective17-routing-learning-2026-03-10"}	2026-03-10 04:59:32.142301+00
2	ops	create_goal	goal	2	\N	Goal created: objective18_prod_probe	{}	2026-03-10 05:09:36.194356+00
3	ops	create_action	action	1	\N	Action recorded for goal 2: achieved	{"goal_id": 2, "validation_status": "achieved"}	2026-03-10 05:09:36.227442+00
4	ops	objective18_promoted	objective	18	objective18-promote-20260310	Objective 18 custody chain promoted to production after PASS gate and rebuild validation	{"release_tag": "objective18-custody-chain-2026-03-10"}	2026-03-10 05:10:43.091321+00
5	tod	create_objective	objective	1	\N	Objective created: Objective18.1 prod objective	{}	2026-03-10 05:26:53.015741+00
6	tod	create_task	task	1	\N	Task created: Objective18.1 prod task	{}	2026-03-10 05:26:53.065821+00
7	tod	create_goal	goal	3	\N	Goal created: o181_prod_probe	{}	2026-03-10 05:26:53.100887+00
8	ops	create_goal	goal	4	\N	Goal created: o181_prod_custody_probe	{}	2026-03-10 05:27:02.226019+00
9	ops	create_action	action	2	\N	Action recorded for goal 4: achieved	{"goal_id": 4, "validation_status": "achieved"}	2026-03-10 05:27:02.250422+00
10	ops	objective18_1_hardening_promoted	objective	18.1	objective18-1-promote-20260310	Objective 18.1 optional FK hardening promoted: invalid->422, missing->404, valid->200	{"release_tag": "objective18.1-optional-fk-hardening-2026-03-10"}	2026-03-10 05:28:39.33236+00
11	ops-gate	create_goal	goal	5	\N	Goal created: o19_prod_probe	{}	2026-03-10 05:57:07.652341+00
12	ops-gate	create_action	action	3	\N	Action recorded for goal 5: achieved	{"goal_id": 5, "validation_status": "achieved"}	2026-03-10 05:57:07.689527+00
13	ops-gate	create_action	action	4	\N	Action recorded for goal 5: failed	{"goal_id": 5, "validation_status": "failed"}	2026-03-10 05:57:07.741119+00
14	ops-gate	create_action	action	5	\N	Action recorded for goal 5: failed	{"goal_id": 5, "validation_status": "failed"}	2026-03-10 05:57:07.783011+00
15	ops-gate	upsert_goal_plan	goal_plan	5	\N	Goal plan updated for goal 5	{"ordered_action_ids": [3, 4, 5], "current_step_index": 1, "derived_status": "partial"}	2026-03-10 05:57:07.820029+00
16	release-bot	promote_objective19	milestone	objective19	objective19-prod-promotion-51263a4f109c	Objective 19 promoted to production and verified	{"release_tag": "objective19-multi-step-execution-2026-03-09", "source_sha": "51263a4f109cae6a9d26c698939092fff8c8b98a", "target_sha": "51263a4f109cae6a9d26c698939092fff8c8b98a", "build_timestamp": "2026-03-10T05:56:03Z", "rollback_point": "51263a4f109cae6a9d26c698939092fff8c8b98a", "deployment_log_entry": "2026-03-10T05:56:03Z release=objective19-multi-step-execution-2026-03-09 git_sha=51263a4f109cae6a9d26c698939092fff8c8b98a", "probe_goal_id": 5}	2026-03-10 05:57:07.964548+00
17	ops-gate	create_goal	goal	6	\N	Goal created: o20_prod_probe	{}	2026-03-10 06:09:30.078467+00
18	ops-gate	create_action	action	6	\N	Action recorded for goal 6: achieved	{"goal_id": 6, "validation_status": "achieved"}	2026-03-10 06:09:30.112367+00
19	ops-gate	create_action	action	7	\N	Action recorded for goal 6: failed	{"goal_id": 6, "validation_status": "failed"}	2026-03-10 06:09:30.145523+00
20	ops-gate	create_action	action	8	\N	Action recorded for goal 6: blocked	{"goal_id": 6, "validation_status": "blocked"}	2026-03-10 06:09:30.169437+00
21	ops-gate	retry_action	action	9	\N	Retried action 7 for goal 6	{"retry_of_action_id": 7, "retry_count": 1}	2026-03-10 06:09:30.1925+00
22	ops-gate	skip_action	action	8	\N	Skipped action 8 for goal 6	{"reason": "intentional_skip"}	2026-03-10 06:09:30.214933+00
23	ops-gate	replace_action	action	10	\N	Replaced action 7 for goal 6	{"replaced_action_id": 7, "replacement_action_id": 10}	2026-03-10 06:09:30.237234+00
24	ops-gate	resume_goal	goal	6	\N	Resumed chain for goal 6	{"resume_action_id": 11}	2026-03-10 06:09:30.260325+00
25	release-bot	promote_objective20	milestone	objective20	objective20-prod-promotion-51263a4f109c	Objective 20 promoted to production and verified	{"release_tag": "objective20-recovery-chain-2026-03-09", "source_sha": "51263a4f109cae6a9d26c698939092fff8c8b98a", "target_sha": "51263a4f109cae6a9d26c698939092fff8c8b98a", "build_timestamp": "2026-03-10T06:09:12Z", "rollback_point": "51263a4f109cae6a9d26c698939092fff8c8b98a", "deployment_log_entry": "2026-03-10T06:09:12Z release=objective20-recovery-chain-2026-03-09 git_sha=51263a4f109cae6a9d26c698939092fff8c8b98a", "probe_goal_id": 6}	2026-03-10 06:09:30.337249+00
26	ops-gate	create_goal	goal	7	\N	Goal created: o20_prod_probe	{}	2026-03-10 06:09:50.128345+00
27	ops-gate	create_action	action	12	\N	Action recorded for goal 7: achieved	{"goal_id": 7, "validation_status": "achieved"}	2026-03-10 06:09:50.151654+00
28	ops-gate	create_action	action	13	\N	Action recorded for goal 7: failed	{"goal_id": 7, "validation_status": "failed"}	2026-03-10 06:09:50.185698+00
29	ops-gate	create_action	action	14	\N	Action recorded for goal 7: blocked	{"goal_id": 7, "validation_status": "blocked"}	2026-03-10 06:09:50.209421+00
30	ops-gate	retry_action	action	15	\N	Retried action 13 for goal 7	{"retry_of_action_id": 13, "retry_count": 1}	2026-03-10 06:09:50.233273+00
31	ops-gate	skip_action	action	14	\N	Skipped action 14 for goal 7	{"reason": "intentional_skip"}	2026-03-10 06:09:50.255475+00
32	ops-gate	replace_action	action	16	\N	Replaced action 13 for goal 7	{"replaced_action_id": 13, "replacement_action_id": 16}	2026-03-10 06:09:50.277126+00
33	ops-gate	resume_goal	goal	7	\N	Resumed chain for goal 7	{"resume_action_id": 17}	2026-03-10 06:09:50.300835+00
34	release-bot	post_release_health_snapshot	milestone	objective20	objective20-postrelease-health-20260310T0610Z	Post-release health snapshot captured after Objective 20 promotion	{"prod_health": "ok", "test_health": "ok", "prod_release_tag": "objective20-recovery-chain-2026-03-09", "prod_schema_version": "2026-03-10-03", "last_backup_file": "/home/testpilot/mim/runtime/prod/backups/mim_prod_20260310T060933Z.sql", "last_health_log": "2026-03-09T23:07:20-07:00 MIM systemd[1]: Finished mim-healthcheck.service - MIM Prod/Test Healthcheck."}	2026-03-10 06:11:02.551904+00
\.


--
-- Data for Name: goal_plans; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.goal_plans (id, goal_id, ordered_action_ids, current_step_index, derived_status, created_at) FROM stdin;
1	5	[3, 4, 5]	1	partial	2026-03-10 05:57:07.689527+00
2	6	[6, 7, 8, 9, 10, 11]	5	partial	2026-03-10 06:09:30.112367+00
3	7	[12, 13, 14, 15, 16, 17]	5	partial	2026-03-10 06:09:50.151654+00
\.


--
-- Data for Name: goals; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.goals (id, objective_id, task_id, goal_type, goal_description, requested_by, priority, status, created_at) FROM stdin;
2	\N	\N	objective18_prod_probe	Post-promotion custody probe	ops	normal	achieved	2026-03-10 05:09:36.194356+00
3	1	1	o181_prod_probe	valid refs	tod	normal	new	2026-03-10 05:26:53.100887+00
4	\N	\N	o181_prod_custody_probe	normal custody probe after 18.1	ops	normal	achieved	2026-03-10 05:27:02.226019+00
5	\N	\N	o19_prod_probe	safe production objective19 verification	ops-gate	normal	partial	2026-03-10 05:57:07.652341+00
6	\N	\N	o20_prod_probe	safe production objective20 verification	ops-gate	normal	partial	2026-03-10 06:09:30.078467+00
7	\N	\N	o20_prod_probe	safe production objective20 verification	ops-gate	normal	partial	2026-03-10 06:09:50.128345+00
\.


--
-- Data for Name: memory_entries; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.memory_entries (id, memory_class, content, summary, metadata_json, created_at) FROM stdin;
\.


--
-- Data for Name: memory_links; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.memory_links (id, source_memory_id, target_memory_id, relation, created_at) FROM stdin;
\.


--
-- Data for Name: objectives; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.objectives (id, title, description, priority, constraints_json, success_criteria, state, created_at) FROM stdin;
1	Objective18.1 prod objective	prod validation	normal	[]	goal creation succeeds	new	2026-03-10 05:26:53.015741+00
\.


--
-- Data for Name: projects; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.projects (id, name, description, created_at) FROM stdin;
\.


--
-- Data for Name: routing_engine_summaries; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.routing_engine_summaries (id, engine_name, runs, pass_rate, review_correction_rate, blocked_rate, avg_latency_ms, fallback_rate, weighted_recent_score, sample_window, created_at) FROM stdin;
1	local	2	1	0	0	43	0	1.97	200	2026-03-10 04:58:20.121414+00
\.


--
-- Data for Name: routing_execution_metrics; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.routing_execution_metrics (id, task_id, objective_id, selected_engine, fallback_engine, fallback_used, routing_source, routing_confidence, policy_version, engine_version, routing_selection_reason, routing_final_outcome, latency_ms, result_category, failure_category, review_outcome, blocked_pre_invocation, metadata_json, created_at) FROM stdin;
1	1	1	local	local	f	tod.invoke-engine	0.55	routing-policy-v1	unknown	active engine from config	success	42	success		pass	f	{"test_results": "not-run", "failures": null, "needs_escalation": false, "tests_run": null}	2026-03-10 04:58:20.106043+00
2	1	1	local	local	f	tod.invoke-engine	0.852	routing-policy-v1	unknown	active engine from config	success	44	success		pass	f	{"failures": null, "test_results": "not-run", "tests_run": null, "needs_escalation": false}	2026-03-10 04:59:14.389724+00
\.


--
-- Data for Name: services; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.services (id, name, status, heartbeat_at, dependency_map, created_at) FROM stdin;
\.


--
-- Data for Name: state_snapshots; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.state_snapshots (id, goal_id, action_id, snapshot_phase, state_type, state_payload, captured_at) FROM stdin;
1	2	1	pre	counter	{"counter": 0}	2026-03-10 05:09:36.227442+00
2	2	1	post	counter	{"counter": 1}	2026-03-10 05:09:36.227442+00
3	4	2	pre	counter	{"counter": 0}	2026-03-10 05:27:02.250422+00
4	4	2	post	counter	{"counter": 1}	2026-03-10 05:27:02.250422+00
5	5	3	pre	counter	{"counter": 0}	2026-03-10 05:57:07.689527+00
6	5	3	post	counter	{"counter": 1}	2026-03-10 05:57:07.689527+00
7	5	4	pre	counter	{"counter": 1}	2026-03-10 05:57:07.741119+00
8	5	4	post	counter	{"counter": 1}	2026-03-10 05:57:07.741119+00
9	5	5	pre	counter	{"counter": 1}	2026-03-10 05:57:07.783011+00
10	5	5	post	counter	{"counter": 1}	2026-03-10 05:57:07.783011+00
11	6	6	pre	counter	{"counter": 0}	2026-03-10 06:09:30.112367+00
12	6	6	post	counter	{"counter": 1}	2026-03-10 06:09:30.112367+00
13	6	7	pre	counter	{"counter": 1}	2026-03-10 06:09:30.145523+00
14	6	7	post	counter	{"counter": 1}	2026-03-10 06:09:30.145523+00
15	6	8	pre	counter	{"counter": 1}	2026-03-10 06:09:30.169437+00
16	6	8	post	counter	{"counter": 1}	2026-03-10 06:09:30.169437+00
17	6	9	pre	counter	{"counter": 1}	2026-03-10 06:09:30.1925+00
18	6	9	post	counter	{"counter": 2}	2026-03-10 06:09:30.1925+00
19	6	10	pre	counter	{"counter": 2}	2026-03-10 06:09:30.237234+00
20	6	10	post	counter	{"counter": 3}	2026-03-10 06:09:30.237234+00
21	6	11	pre	resume	{"goal_id": 6}	2026-03-10 06:09:30.260325+00
22	6	11	post	resume	{"goal_id": 6, "resumed": true}	2026-03-10 06:09:30.260325+00
23	7	12	pre	counter	{"counter": 0}	2026-03-10 06:09:50.151654+00
24	7	12	post	counter	{"counter": 1}	2026-03-10 06:09:50.151654+00
25	7	13	pre	counter	{"counter": 1}	2026-03-10 06:09:50.185698+00
26	7	13	post	counter	{"counter": 1}	2026-03-10 06:09:50.185698+00
27	7	14	pre	counter	{"counter": 1}	2026-03-10 06:09:50.209421+00
28	7	14	post	counter	{"counter": 1}	2026-03-10 06:09:50.209421+00
29	7	15	pre	counter	{"counter": 1}	2026-03-10 06:09:50.233273+00
30	7	15	post	counter	{"counter": 2}	2026-03-10 06:09:50.233273+00
31	7	16	pre	counter	{"counter": 2}	2026-03-10 06:09:50.277126+00
32	7	16	post	counter	{"counter": 3}	2026-03-10 06:09:50.277126+00
33	7	17	pre	resume	{"goal_id": 7}	2026-03-10 06:09:50.300835+00
34	7	17	post	resume	{"goal_id": 7, "resumed": true}	2026-03-10 06:09:50.300835+00
\.


--
-- Data for Name: task_results; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.task_results (id, task_id, result, files_changed, tests_run, test_results, failures, recommendations, created_at) FROM stdin;
\.


--
-- Data for Name: task_reviews; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.task_reviews (id, task_id, reviewer, status, notes, continue_allowed, escalate_to_user, created_at) FROM stdin;
\.


--
-- Data for Name: tasks; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.tasks (id, objective_id, title, details, dependencies, acceptance_criteria, assigned_to, state, created_at) FROM stdin;
1	1	Objective18.1 prod task	prod validation	[]	goal refs valid	ops	queued	2026-03-10 05:26:53.065821+00
\.


--
-- Data for Name: tool_invocations; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.tool_invocations (id, tool_id, actor, input_json, output_json, status, created_at) FROM stdin;
\.


--
-- Data for Name: tools; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.tools (id, name, description, enabled, created_at) FROM stdin;
\.


--
-- Data for Name: validation_results; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.validation_results (id, goal_id, action_id, validation_method, validation_status, validation_details, validated_at) FROM stdin;
1	2	1	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1}	2026-03-10 05:09:36.227442+00
2	4	2	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1}	2026-03-10 05:27:02.250422+00
3	5	3	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1}	2026-03-10 05:57:07.689527+00
4	5	4	expected_delta_compare	failed	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 0.0}, "checks": {"counter": {"expected": 1, "observed": 0.0, "match": false}}, "matched": 0, "total": 1}	2026-03-10 05:57:07.741119+00
5	5	5	expected_delta_compare	failed	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 0.0}, "checks": {"counter": {"expected": 1, "observed": 0.0, "match": false}}, "matched": 0, "total": 1}	2026-03-10 05:57:07.783011+00
6	6	6	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1}	2026-03-10 06:09:30.112367+00
7	6	7	expected_delta_compare	failed	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 0.0}, "checks": {"counter": {"expected": 1, "observed": 0.0, "match": false}}, "matched": 0, "total": 1}	2026-03-10 06:09:30.145523+00
8	6	8	expected_delta_compare	blocked	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 0.0}, "reason": "action_status_blocked"}	2026-03-10 06:09:30.169437+00
9	6	9	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1, "timeline_marker": "retry", "retry_of_action_id": 7}	2026-03-10 06:09:30.1925+00
10	6	8	manual_skip	skipped	{"timeline_marker": "skip", "reason": "intentional_skip", "continue_to_next_step": true}	2026-03-10 06:09:30.214933+00
11	6	10	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1, "timeline_marker": "replace", "replaced_action_id": 7}	2026-03-10 06:09:30.237234+00
12	6	11	manual_resume	achieved	{"timeline_marker": "resume", "recovery_classification": "recovered"}	2026-03-10 06:09:30.260325+00
13	7	12	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1}	2026-03-10 06:09:50.151654+00
14	7	13	expected_delta_compare	failed	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 0.0}, "checks": {"counter": {"expected": 1, "observed": 0.0, "match": false}}, "matched": 0, "total": 1}	2026-03-10 06:09:50.185698+00
15	7	14	expected_delta_compare	blocked	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 0.0}, "reason": "action_status_blocked"}	2026-03-10 06:09:50.209421+00
16	7	15	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1, "timeline_marker": "retry", "retry_of_action_id": 13}	2026-03-10 06:09:50.233273+00
17	7	14	manual_skip	skipped	{"timeline_marker": "skip", "reason": "intentional_skip", "continue_to_next_step": true}	2026-03-10 06:09:50.255475+00
18	7	16	expected_delta_compare	achieved	{"expected_state_delta": {"counter": 1}, "observed_state_delta": {"counter": 1.0}, "checks": {"counter": {"expected": 1, "observed": 1.0, "match": true}}, "matched": 1, "total": 1, "timeline_marker": "replace", "replaced_action_id": 13}	2026-03-10 06:09:50.277126+00
19	7	17	manual_resume	achieved	{"timeline_marker": "resume", "recovery_classification": "recovered"}	2026-03-10 06:09:50.300835+00
\.


--
-- Name: actions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.actions_id_seq', 17, true);


--
-- Name: actors_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.actors_id_seq', 1, false);


--
-- Name: execution_journal_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.execution_journal_id_seq', 34, true);


--
-- Name: goal_plans_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.goal_plans_id_seq', 3, true);


--
-- Name: goals_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.goals_id_seq', 7, true);


--
-- Name: memory_entries_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.memory_entries_id_seq', 1, false);


--
-- Name: memory_links_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.memory_links_id_seq', 1, false);


--
-- Name: objectives_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.objectives_id_seq', 1, true);


--
-- Name: projects_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.projects_id_seq', 1, false);


--
-- Name: routing_engine_summaries_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.routing_engine_summaries_id_seq', 1, true);


--
-- Name: routing_execution_metrics_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.routing_execution_metrics_id_seq', 2, true);


--
-- Name: services_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.services_id_seq', 1, false);


--
-- Name: state_snapshots_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.state_snapshots_id_seq', 34, true);


--
-- Name: task_results_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.task_results_id_seq', 1, false);


--
-- Name: task_reviews_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.task_reviews_id_seq', 1, false);


--
-- Name: tasks_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.tasks_id_seq', 1, true);


--
-- Name: tool_invocations_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.tool_invocations_id_seq', 1, false);


--
-- Name: tools_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.tools_id_seq', 1, false);


--
-- Name: validation_results_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.validation_results_id_seq', 19, true);


--
-- Name: actions actions_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.actions
    ADD CONSTRAINT actions_pkey PRIMARY KEY (id);


--
-- Name: actors actors_name_key; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.actors
    ADD CONSTRAINT actors_name_key UNIQUE (name);


--
-- Name: actors actors_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.actors
    ADD CONSTRAINT actors_pkey PRIMARY KEY (id);


--
-- Name: execution_journal execution_journal_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.execution_journal
    ADD CONSTRAINT execution_journal_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: execution_journal execution_journal_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.execution_journal
    ADD CONSTRAINT execution_journal_pkey PRIMARY KEY (id);


--
-- Name: goal_plans goal_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.goal_plans
    ADD CONSTRAINT goal_plans_pkey PRIMARY KEY (id);


--
-- Name: goals goals_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.goals
    ADD CONSTRAINT goals_pkey PRIMARY KEY (id);


--
-- Name: memory_entries memory_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.memory_entries
    ADD CONSTRAINT memory_entries_pkey PRIMARY KEY (id);


--
-- Name: memory_links memory_links_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.memory_links
    ADD CONSTRAINT memory_links_pkey PRIMARY KEY (id);


--
-- Name: objectives objectives_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.objectives
    ADD CONSTRAINT objectives_pkey PRIMARY KEY (id);


--
-- Name: projects projects_name_key; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_name_key UNIQUE (name);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: routing_engine_summaries routing_engine_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.routing_engine_summaries
    ADD CONSTRAINT routing_engine_summaries_pkey PRIMARY KEY (id);


--
-- Name: routing_execution_metrics routing_execution_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.routing_execution_metrics
    ADD CONSTRAINT routing_execution_metrics_pkey PRIMARY KEY (id);


--
-- Name: services services_name_key; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.services
    ADD CONSTRAINT services_name_key UNIQUE (name);


--
-- Name: services services_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.services
    ADD CONSTRAINT services_pkey PRIMARY KEY (id);


--
-- Name: state_snapshots state_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.state_snapshots
    ADD CONSTRAINT state_snapshots_pkey PRIMARY KEY (id);


--
-- Name: task_results task_results_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.task_results
    ADD CONSTRAINT task_results_pkey PRIMARY KEY (id);


--
-- Name: task_reviews task_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.task_reviews
    ADD CONSTRAINT task_reviews_pkey PRIMARY KEY (id);


--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);


--
-- Name: tool_invocations tool_invocations_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tool_invocations
    ADD CONSTRAINT tool_invocations_pkey PRIMARY KEY (id);


--
-- Name: tools tools_name_key; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tools
    ADD CONSTRAINT tools_name_key UNIQUE (name);


--
-- Name: tools tools_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tools
    ADD CONSTRAINT tools_pkey PRIMARY KEY (id);


--
-- Name: validation_results validation_results_pkey; Type: CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.validation_results
    ADD CONSTRAINT validation_results_pkey PRIMARY KEY (id);


--
-- Name: ix_actions_goal_id; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_actions_goal_id ON public.actions USING btree (goal_id);


--
-- Name: ix_goal_plans_goal_id; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE UNIQUE INDEX ix_goal_plans_goal_id ON public.goal_plans USING btree (goal_id);


--
-- Name: ix_memory_entries_memory_class; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_memory_entries_memory_class ON public.memory_entries USING btree (memory_class);


--
-- Name: ix_objectives_title; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_objectives_title ON public.objectives USING btree (title);


--
-- Name: ix_routing_engine_summaries_engine_name; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE UNIQUE INDEX ix_routing_engine_summaries_engine_name ON public.routing_engine_summaries USING btree (engine_name);


--
-- Name: ix_routing_execution_metrics_selected_engine; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_routing_execution_metrics_selected_engine ON public.routing_execution_metrics USING btree (selected_engine);


--
-- Name: ix_state_snapshots_action_id; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_state_snapshots_action_id ON public.state_snapshots USING btree (action_id);


--
-- Name: ix_state_snapshots_goal_id; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_state_snapshots_goal_id ON public.state_snapshots USING btree (goal_id);


--
-- Name: ix_state_snapshots_snapshot_phase; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_state_snapshots_snapshot_phase ON public.state_snapshots USING btree (snapshot_phase);


--
-- Name: ix_tasks_title; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_tasks_title ON public.tasks USING btree (title);


--
-- Name: ix_validation_results_action_id; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_validation_results_action_id ON public.validation_results USING btree (action_id);


--
-- Name: ix_validation_results_goal_id; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_validation_results_goal_id ON public.validation_results USING btree (goal_id);


--
-- Name: actions actions_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.actions
    ADD CONSTRAINT actions_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id) ON DELETE CASCADE;


--
-- Name: goal_plans goal_plans_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.goal_plans
    ADD CONSTRAINT goal_plans_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id) ON DELETE CASCADE;


--
-- Name: goals goals_objective_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.goals
    ADD CONSTRAINT goals_objective_id_fkey FOREIGN KEY (objective_id) REFERENCES public.objectives(id) ON DELETE SET NULL;


--
-- Name: goals goals_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.goals
    ADD CONSTRAINT goals_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: memory_links memory_links_source_memory_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.memory_links
    ADD CONSTRAINT memory_links_source_memory_id_fkey FOREIGN KEY (source_memory_id) REFERENCES public.memory_entries(id) ON DELETE CASCADE;


--
-- Name: memory_links memory_links_target_memory_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.memory_links
    ADD CONSTRAINT memory_links_target_memory_id_fkey FOREIGN KEY (target_memory_id) REFERENCES public.memory_entries(id) ON DELETE CASCADE;


--
-- Name: state_snapshots state_snapshots_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.state_snapshots
    ADD CONSTRAINT state_snapshots_action_id_fkey FOREIGN KEY (action_id) REFERENCES public.actions(id) ON DELETE CASCADE;


--
-- Name: state_snapshots state_snapshots_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.state_snapshots
    ADD CONSTRAINT state_snapshots_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id) ON DELETE CASCADE;


--
-- Name: task_results task_results_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.task_results
    ADD CONSTRAINT task_results_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;


--
-- Name: task_reviews task_reviews_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.task_reviews
    ADD CONSTRAINT task_reviews_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;


--
-- Name: tasks tasks_objective_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_objective_id_fkey FOREIGN KEY (objective_id) REFERENCES public.objectives(id) ON DELETE SET NULL;


--
-- Name: tool_invocations tool_invocations_tool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.tool_invocations
    ADD CONSTRAINT tool_invocations_tool_id_fkey FOREIGN KEY (tool_id) REFERENCES public.tools(id) ON DELETE CASCADE;


--
-- Name: validation_results validation_results_action_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.validation_results
    ADD CONSTRAINT validation_results_action_id_fkey FOREIGN KEY (action_id) REFERENCES public.actions(id) ON DELETE CASCADE;


--
-- Name: validation_results validation_results_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.validation_results
    ADD CONSTRAINT validation_results_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict xcAfotmxvgWsv0A38cQfpsFLVdUaROEjhT8gIh3jM3falDQEXaV86SzNBnRmLLf

