-- WARNING: This schema is for context only and is not meant to be run.
-- Table order and constraints may not be valid for execution.

CREATE TABLE public.doctors (
  id integer NOT NULL DEFAULT nextval('doctors_id_seq'::regclass),
  username character varying NOT NULL UNIQUE,
  codice_fiscale character varying NOT NULL UNIQUE,
  password_hash character varying NOT NULL,
  created_at timestamp without time zone DEFAULT now(),
  CONSTRAINT doctors_pkey PRIMARY KEY (id)
);
CREATE TABLE public.measurements (
  id integer NOT NULL DEFAULT nextval('measurements_id_seq'::regclass),
  codice_fiscale character varying NOT NULL,
  timestamp timestamp without time zone NOT NULL,
  motor_updrs numeric,
  jitter numeric,
  shimmer numeric,
  created_at timestamp without time zone DEFAULT now(),
  hnr double precision,
  nhr double precision,
  dfa double precision,
  ppe double precision,
  note_medico text,
  CONSTRAINT measurements_pkey PRIMARY KEY (id),
  CONSTRAINT measurements_codice_fiscale_fkey FOREIGN KEY (codice_fiscale) REFERENCES public.patients(codice_fiscale)
);
CREATE TABLE public.patients (
  id integer NOT NULL DEFAULT nextval('patients_id_seq'::regclass),
  codice_fiscale character varying NOT NULL UNIQUE,
  nome character varying NOT NULL,
  cognome character varying NOT NULL,
  password_hash character varying NOT NULL,
  age integer,
  sex integer,
  baseline_updrs numeric,
  baseline_date timestamp without time zone,
  doctor_username character varying NOT NULL,
  created_at timestamp without time zone DEFAULT now(),
  CONSTRAINT patients_pkey PRIMARY KEY (id),
  CONSTRAINT patients_doctor_username_fkey FOREIGN KEY (doctor_username) REFERENCES public.doctors(username)
);
