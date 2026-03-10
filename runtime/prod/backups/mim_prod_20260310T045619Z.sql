--
-- PostgreSQL database dump
--

\restrict lEE3AoHnMja5bVyNpLcluU9YFIeXVmHYM1Ob7kfmqauTCGnlMwk2gPygPmNcGH7

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
-- Name: actors id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.actors ALTER COLUMN id SET DEFAULT nextval('public.actors_id_seq'::regclass);


--
-- Name: execution_journal id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.execution_journal ALTER COLUMN id SET DEFAULT nextval('public.execution_journal_id_seq'::regclass);


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
-- Name: services id; Type: DEFAULT; Schema: public; Owner: mim_prod
--

ALTER TABLE ONLY public.services ALTER COLUMN id SET DEFAULT nextval('public.services_id_seq'::regclass);


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
-- Data for Name: actors; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.actors (id, name, role, identity_metadata, created_at) FROM stdin;
\.


--
-- Data for Name: execution_journal; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.execution_journal (id, actor, action, target_type, target_id, idempotency_key, result, metadata_json, created_at) FROM stdin;
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
\.


--
-- Data for Name: projects; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.projects (id, name, description, created_at) FROM stdin;
\.


--
-- Data for Name: services; Type: TABLE DATA; Schema: public; Owner: mim_prod
--

COPY public.services (id, name, status, heartbeat_at, dependency_map, created_at) FROM stdin;
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
-- Name: actors_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.actors_id_seq', 1, false);


--
-- Name: execution_journal_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.execution_journal_id_seq', 1, false);


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

SELECT pg_catalog.setval('public.objectives_id_seq', 1, false);


--
-- Name: projects_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.projects_id_seq', 1, false);


--
-- Name: services_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.services_id_seq', 1, false);


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

SELECT pg_catalog.setval('public.tasks_id_seq', 1, false);


--
-- Name: tool_invocations_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.tool_invocations_id_seq', 1, false);


--
-- Name: tools_id_seq; Type: SEQUENCE SET; Schema: public; Owner: mim_prod
--

SELECT pg_catalog.setval('public.tools_id_seq', 1, false);


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
-- Name: ix_memory_entries_memory_class; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_memory_entries_memory_class ON public.memory_entries USING btree (memory_class);


--
-- Name: ix_objectives_title; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_objectives_title ON public.objectives USING btree (title);


--
-- Name: ix_tasks_title; Type: INDEX; Schema: public; Owner: mim_prod
--

CREATE INDEX ix_tasks_title ON public.tasks USING btree (title);


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
-- PostgreSQL database dump complete
--

\unrestrict lEE3AoHnMja5bVyNpLcluU9YFIeXVmHYM1Ob7kfmqauTCGnlMwk2gPygPmNcGH7

