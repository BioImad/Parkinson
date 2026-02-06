import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import numpy as np
import hashlib
import uuid
import os
from datetime import datetime
from supabase import create_client, Client
import parselmouth
from pathlib import Path
import tempfile

st.set_page_config(page_title="Parkinson Telemonitoring", layout="wide")

# ==================== CONFIGURAZIONE SUPABASE ====================
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "https://viexdcbofgsopcrnnbzi.supabase.co")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY",
                              "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZpZXhkY2JvZmdzb3Bjcm5uYnppIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Njk1ODk4OTUsImV4cCI6MjA4NTE2NTg5NX0.7Xu5B8Vlz0j-wX39-i5W12Mw5cedX7VS9ACOPjSpLEs")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ==================== FUNZIONI BACKEND ====================

def extract_vocal_features(audio_path):
    """Estrae le 6 feature vocali necessarie"""
    try:
        sound = parselmouth.Sound(str(audio_path))
        point_process = parselmouth.praat.call(sound, "To PointProcess (periodic, cc)", 75, 500)

        jitter_abs = parselmouth.praat.call(
            point_process, "Get jitter (local, absolute)", 0, 0, 0.0001, 0.02, 1.3
        )

        shimmer_local = parselmouth.praat.call(
            [sound, point_process], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6
        )

        harmonicity = parselmouth.praat.call(sound, "To Harmonicity (cc)", 0.01, 75, 0.1, 1.0)
        hnr = parselmouth.praat.call(harmonicity, "Get mean", 0, 0)
        nhr = 1.0 / (hnr + 1e-6) if hnr > 0 else 1.0

        intensity = sound.to_intensity(time_step=0.01)
        intensity_values = [
            intensity.get_value(t) for t in intensity.xs()
            if not np.isnan(intensity.get_value(t))
        ]
        dfa = np.std(intensity_values) / (np.mean(intensity_values) + 1e-6) if len(intensity_values) > 10 else 0.0

        pitch = sound.to_pitch(time_step=0.01, pitch_floor=75, pitch_ceiling=500)
        pitch_values = [
            pitch.get_value_at_time(t) for t in pitch.xs()
            if not np.isnan(pitch.get_value_at_time(t))
        ]
        if len(pitch_values) > 5:
            pitch_diffs = np.diff(pitch_values)
            ppe = np.std(pitch_diffs) / (np.mean(np.abs(pitch_diffs)) + 1e-6)
        else:
            ppe = 0.0

        return {
            'jitter_abs': float(jitter_abs),
            'shimmer_local': float(shimmer_local),
            'hnr': float(hnr),
            'nhr': float(nhr),
            'dfa': float(dfa),
            'ppe': float(ppe)
        }

    except Exception as e:
        st.error(f"Errore analisi audio: {str(e)}")
        return None


def compute_updrs(features):
    """Calcola UPDRS con normalizzazione"""
    MEANS = {
        'jitter_abs': 0.00004, 'shimmer_local': 0.030, 'nhr': 0.025,
        'hnr': 21.7, 'dfa': 0.718, 'ppe': 0.206
    }
    STDS = {
        'jitter_abs': 0.00006, 'shimmer_local': 0.018, 'nhr': 0.040,
        'hnr': 4.3, 'dfa': 0.055, 'ppe': 0.090
    }

    jitter_norm = (features['jitter_abs'] - MEANS['jitter_abs']) / STDS['jitter_abs']
    shimmer_norm = (features['shimmer_local'] - MEANS['shimmer_local']) / STDS['shimmer_local']
    nhr_norm = (features['nhr'] - MEANS['nhr']) / STDS['nhr']
    hnr_norm = (features['hnr'] - MEANS['hnr']) / STDS['hnr']
    dfa_norm = (features['dfa'] - MEANS['dfa']) / STDS['dfa']
    ppe_norm = (features['ppe'] - MEANS['ppe']) / STDS['ppe']

    updrs = (
            21.0 +
            3.2 * jitter_norm +
            2.8 * shimmer_norm +
            2.5 * nhr_norm +
            -1.8 * hnr_norm +
            2.1 * dfa_norm +
            1.9 * ppe_norm
    )

    return max(0.0, min(108.0, round(updrs, 2)))


def login_doctor(username, password):
    """Login medico"""
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    username_upper = username.upper()

    try:
        response = supabase.table("doctors").select("*").eq("username", username).eq("password_hash", pw_hash).execute()
        if not response.data:
            response = supabase.table("doctors").select("*").eq("codice_fiscale", username_upper).eq("password_hash",
                                                                                                     pw_hash).execute()

        if response.data:
            return response.data[0]
        return None
    except Exception as e:
        st.error(f"Errore login: {str(e)}")
        return None


def login_patient(codice_fiscale, password):
    """Login paziente"""
    pw_hash = hashlib.sha256(password.encode()).hexdigest()

    try:
        response = supabase.table("patients").select("*").eq(
            "codice_fiscale", codice_fiscale.upper()
        ).eq("password_hash", pw_hash).execute()

        if response.data:
            return response.data[0]
        return None
    except Exception as e:
        st.error(f"Errore login: {str(e)}")
        return None


def register_patient(codice_fiscale, nome, cognome, password, age, sex, doctor_username):
    """Registra nuovo paziente"""
    import re

    cf_upper = codice_fiscale.upper()

    if not re.match(r'^[A-Z0-9]{16}$', cf_upper):
        return False, "Codice fiscale non valido"

    pw_hash = hashlib.sha256(password.encode()).hexdigest()

    try:
        supabase.table("patients").insert({
            "codice_fiscale": cf_upper,
            "nome": nome,
            "cognome": cognome,
            "password_hash": pw_hash,
            "age": age,
            "sex": 1 if sex == "M" else 0,
            "doctor_username": doctor_username,
            "baseline_date": datetime.now().isoformat()
        }).execute()

        return True, f"Paziente {nome} {cognome} registrato"
    except Exception as e:
        if "duplicate" in str(e).lower():
            return False, "Codice fiscale già registrato"
        return False, str(e)


def get_patients(doctor_username):
    """Lista pazienti del medico"""
    try:
        response = supabase.table("patients").select(
            "codice_fiscale, nome, cognome, age, sex, doctor_username"
        ).eq("doctor_username", doctor_username).execute()
        return response.data
    except Exception as e:
        st.error(f"Errore caricamento pazienti: {str(e)}")
        return []


def get_history(codice_fiscale):
    """Storico misurazioni paziente"""
    cf_upper = codice_fiscale.upper()

    try:
        patient_response = supabase.table("patients").select("*").eq(
            "codice_fiscale", cf_upper
        ).execute()

        if not patient_response.data:
            return None, None

        info = patient_response.data[0]

        measurements_response = supabase.table("measurements").select("*").eq(
            "codice_fiscale", cf_upper
        ).order("timestamp", desc=False).execute()

        return info, measurements_response.data
    except Exception as e:
        st.error(f"Errore caricamento storico: {str(e)}")
        return None, None


def process_visit(codice_fiscale, audio_file):
    """Processa visita con analisi vocale"""
    cf_upper = codice_fiscale.upper()

    # Salva temporaneamente il file audio
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
        tmp_file.write(audio_file.getvalue())
        temp_path = tmp_file.name

    try:
        # Verifica paziente
        patient_check = supabase.table("patients").select("*").eq(
            "codice_fiscale", cf_upper
        ).execute()

        if not patient_check.data:
            return None, "Paziente non trovato"

        # Estrai features
        features = extract_vocal_features(temp_path)
        if not features:
            return None, "Errore nell'analisi audio"

        # Calcola UPDRS
        updrs = compute_updrs(features)

        # Salva misurazione
        supabase.table("measurements").insert({
            "codice_fiscale": cf_upper,
            "timestamp": datetime.now().isoformat(),
            "motor_updrs": updrs,
            "jitter": features['jitter_abs'],
            "shimmer": features['shimmer_local'],
            "hnr": features['hnr'],
            "nhr": features['nhr'],
            "dfa": features['dfa'],
            "ppe": features['ppe'],
            "note_medico": None
        }).execute()

        # Aggiorna baseline se prima misurazione
        patient = patient_check.data[0]
        if not patient.get("baseline_updrs"):
            supabase.table("patients").update({
                "baseline_updrs": updrs
            }).eq("codice_fiscale", cf_upper).execute()

        result = {
            "motor_UPDRS": updrs,
            "jitter": features['jitter_abs'],
            "shimmer": features['shimmer_local'],
            "hnr": features['hnr'],
            "nhr": features['nhr'],
            "dfa": features['dfa'],
            "ppe": features['ppe']
        }

        return result, None

    except Exception as e:
        return None, str(e)
    finally:
        # Rimuovi file temporaneo
        if os.path.exists(temp_path):
            os.remove(temp_path)


def add_note(codice_fiscale, timestamp, note, doctor_username):
    """Aggiungi nota del medico"""
    cf_upper = codice_fiscale.upper()

    try:
        # Verifica autorizzazione
        patient_check = supabase.table("patients").select("*").eq(
            "codice_fiscale", cf_upper
        ).eq("doctor_username", doctor_username).execute()

        if not patient_check.data:
            return False, "Non autorizzato"

        # Aggiorna nota
        supabase.table("measurements").update({
            "note_medico": note
        }).eq("codice_fiscale", cf_upper).eq("timestamp", timestamp).execute()

        return True, "Nota salvata"
    except Exception as e:
        return False, str(e)


def get_patient_stats(codice_fiscale):
    """Statistiche paziente"""
    cf_upper = codice_fiscale.upper()

    try:
        measurements = supabase.table("measurements").select("*").eq(
            "codice_fiscale", cf_upper
        ).order("timestamp", desc=False).execute()

        if not measurements.data or len(measurements.data) == 0:
            return {
                "n_misurazioni": 0,
                "ultimo_updrs": None,
                "primo_updrs": None,
                "variazione": None,
                "trend": None
            }

        data = measurements.data
        updrs_values = [m['motor_updrs'] for m in data]

        return {
            "n_misurazioni": len(data),
            "ultimo_updrs": updrs_values[-1],
            "primo_updrs": updrs_values[0],
            "variazione": updrs_values[-1] - updrs_values[0],
            "trend": "peggioramento" if updrs_values[-1] > updrs_values[0] else "miglioramento"
        }
    except Exception as e:
        st.error(f"Errore statistiche: {str(e)}")
        return None


def get_doctor_overview(doctor_username):
    """Overview per il medico"""
    try:
        patients = supabase.table("patients").select("*").eq(
            "doctor_username", doctor_username
        ).execute()

        if not patients.data:
            return {
                "n_pazienti": 0,
                "pazienti_critici": [],
                "trend_generale": None
            }

        pazienti_critici = []
        all_trends = []

        for patient in patients.data:
            cf = patient['codice_fiscale']

            measurements = supabase.table("measurements").select("motor_updrs").eq(
                "codice_fiscale", cf
            ).order("timestamp", desc=False).execute()

            if measurements.data and len(measurements.data) >= 2:
                updrs_vals = [m['motor_updrs'] for m in measurements.data]
                variazione = updrs_vals[-1] - updrs_vals[0]
                all_trends.append(variazione)

                if updrs_vals[-1] > 30 or variazione > 10:
                    pazienti_critici.append({
                        "nome": f"{patient['nome']} {patient['cognome']}",
                        "codice_fiscale": cf,
                        "ultimo_updrs": updrs_vals[-1],
                        "variazione": variazione
                    })

        trend_medio = np.mean(all_trends) if all_trends else 0

        return {
            "n_pazienti": len(patients.data),
            "pazienti_critici": sorted(pazienti_critici, key=lambda x: x['ultimo_updrs'], reverse=True),
            "trend_generale": round(trend_medio, 2)
        }
    except Exception as e:
        st.error(f"Errore overview: {str(e)}")
        return None


def reset_patient_password(doctor_username, codice_fiscale_paziente, new_password):
    """Reset password paziente"""
    cf_upper = codice_fiscale_paziente.upper()

    try:
        patient_check = supabase.table("patients").select("*").eq(
            "codice_fiscale", cf_upper
        ).eq("doctor_username", doctor_username).execute()

        if not patient_check.data:
            return False, "Paziente non trovato o non appartiene a questo medico"

        pw_hash = hashlib.sha256(new_password.encode()).hexdigest()

        supabase.table("patients").update({
            "password_hash": pw_hash
        }).eq("codice_fiscale", cf_upper).execute()

        patient = patient_check.data[0]
        return True, f"{patient['nome']} {patient['cognome']}"

    except Exception as e:
        return False, str(e)


# ==================== FUNZIONI GRAFICI ====================

def create_updrs_trend_chart_simple(df):
    """Grafico UPDRS semplificato per paziente - SENZA zone colorate"""
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=df['timestamp'],
        y=df['motor_updrs'],
        mode='lines+markers',
        name='UPDRS Motorio',
        line=dict(color='#4A90E2', width=4),
        marker=dict(size=10, color='#4A90E2'),
        hovertemplate='<b>Data</b>: %{x|%d/%m/%Y}<br><b>UPDRS</b>: %{y:.1f}<extra></extra>'
    ))

    fig.update_layout(
        title={
            'text': "Andamento del tuo UPDRS nel Tempo",
            'font': {'size': 20, 'color': '#333'}
        },
        xaxis_title="Data",
        yaxis_title="Punteggio UPDRS",
        hovermode='closest',
        height=450,
        plot_bgcolor='white',
        paper_bgcolor='white',
        font=dict(size=14),
        xaxis=dict(showgrid=True, gridcolor='#E0E0E0'),
        yaxis=dict(showgrid=True, gridcolor='#E0E0E0')
    )

    return fig


def create_updrs_trend_chart_medico(df):
    """Grafico UPDRS per medico - CON zone colorate"""
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=df['timestamp'],
        y=df['motor_updrs'],
        mode='lines+markers',
        name='UPDRS Motorio',
        line=dict(color='#1f77b4', width=3),
        marker=dict(size=8)
    ))

    # Zone di riferimento clinico
    fig.add_hrect(y0=0, y1=20, fillcolor="green", opacity=0.1, line_width=0, annotation_text="Lieve")
    fig.add_hrect(y0=20, y1=40, fillcolor="yellow", opacity=0.1, line_width=0, annotation_text="Moderato")
    fig.add_hrect(y0=40, y1=108, fillcolor="red", opacity=0.1, line_width=0, annotation_text="Severo")

    fig.update_layout(
        title="Evoluzione UPDRS Motorio nel Tempo",
        xaxis_title="Data",
        yaxis_title="Punteggio UPDRS",
        hovermode='x unified',
        height=400
    )

    return fig


# ==================== INIZIALIZZAZIONE SESSIONE ====================

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.user = None
    st.session_state.role = None
    st.session_state.selected_role = None

# ==================== SELEZIONE RUOLO ====================

if not st.session_state.selected_role and not st.session_state.logged_in:
    st.title("Portale Telemonitoring Parkinson")
    st.markdown("### Seleziona il tuo ruolo")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sono un Medico", use_container_width=True, type="primary"):
            st.session_state.selected_role = "medico"
            st.rerun()
    with col2:
        if st.button("Sono un Paziente", use_container_width=True):
            st.session_state.selected_role = "paziente"
            st.rerun()
    st.stop()

# ==================== LOGIN MEDICO ====================

if st.session_state.selected_role == "medico" and not st.session_state.logged_in:
    st.title("Accesso Medico")

    if st.button("← Indietro"):
        st.session_state.selected_role = None
        st.rerun()

    with st.form("login_medico"):
        username = st.text_input("Username o Codice Fiscale")
        password = st.text_input("Password", type="password")

        if st.form_submit_button("Accedi", use_container_width=True):
            user = login_doctor(username, password)
            if user:
                st.session_state.update({
                    "logged_in": True,
                    "user": user["username"],
                    "role": "medico"
                })
                st.rerun()
            else:
                st.error("Credenziali non valide")

    st.stop()

# ==================== LOGIN PAZIENTE ====================

if st.session_state.selected_role == "paziente" and not st.session_state.logged_in:
    st.title("Accesso Paziente")

    if st.button("← Indietro"):
        st.session_state.selected_role = None
        st.rerun()

    with st.form("login_paziente"):
        codice_fiscale = st.text_input("Codice Fiscale").upper()
        password = st.text_input("Password", type="password")

        if st.form_submit_button("Accedi", use_container_width=True):
            patient = login_patient(codice_fiscale, password)
            if patient:
                st.session_state.update({
                    "logged_in": True,
                    "user": codice_fiscale,
                    "nome_completo": f"{patient.get('nome', '')} {patient.get('cognome', '')}",
                    "role": "paziente"
                })
                st.rerun()
            else:
                st.error("Credenziali non valide")

    st.stop()

# ==================== DASHBOARD MEDICO ====================

if st.session_state.role == "medico":
    st.sidebar.title(f"Dr. {st.session_state.user}")
    if st.sidebar.button("Logout"):
        st.session_state.logged_in = False
        st.session_state.user = None
        st.session_state.selected_role = None
        st.rerun()

    # Overview dashboard medico
    overview = get_doctor_overview(st.session_state.user)
    if overview:
        col1, col2, col3 = st.columns(3)
        col1.metric("Pazienti in Carico", overview['n_pazienti'])
        col2.metric("Pazienti Critici", len(overview['pazienti_critici']))

        if overview['pazienti_critici']:
            st.warning("Attenzione: pazienti che richiedono monitoraggio ravvicinato")
            for p in overview['pazienti_critici'][:3]:
                st.write(f"• {p['nome']} - UPDRS: {p['ultimo_updrs']:.1f} (Δ {p['variazione']:+.1f})")

    st.title("Area Medico")
    menu = st.tabs(["Registra Paziente", "Esegui Visita", "Archivio Pazienti", "Reset Password"])

    # TAB 1: Registrazione
    with menu[0]:
        st.subheader("Registra Nuovo Paziente")
        with st.form("registra_paziente"):
            col1, col2 = st.columns(2)
            with col1:
                nome = st.text_input("Nome")
                cognome = st.text_input("Cognome")
                codice_fiscale = st.text_input("Codice Fiscale (16 caratteri)").upper()
            with col2:
                age = st.number_input("Età", 18, 100, value=65)
                sex = st.selectbox("Sesso", ["M", "F"])
                password = st.text_input("Password iniziale", type="password")

            if st.form_submit_button("Registra"):
                if nome and cognome and codice_fiscale and password:
                    success, message = register_patient(
                        codice_fiscale, nome, cognome, password, age, sex, st.session_state.user
                    )
                    if success:
                        st.success(message)
                        st.info(f"Credenziali:\n- CF: `{codice_fiscale}`\n- Password: `{password}`")
                    else:
                        st.error(message)
                else:
                    st.warning("Compila tutti i campi")

    # TAB 2: Visita
    with menu[1]:
        st.subheader("Esegui Visita e Analisi Vocale")
        with st.form("visita"):
            codice_fiscale_visita = st.text_input("Codice Fiscale Paziente").upper()
            audio = st.file_uploader("Registrazione Vocale (.wav)", type=["wav"])

            if st.form_submit_button("Analizza"):
                if audio and codice_fiscale_visita:
                    with st.spinner("Analisi in corso..."):
                        result, error = process_visit(codice_fiscale_visita, audio)

                        if result:
                            st.success("Analisi completata")

                            col1, col2, col3, col4 = st.columns(4)
                            col1.metric("UPDRS Motorio", f"{result['motor_UPDRS']:.1f}")
                            col2.metric("Jitter", f"{result['jitter']:.6f}")
                            col3.metric("Shimmer", f"{result['shimmer']:.6f}")
                            col4.metric("HNR", f"{result['hnr']:.2f}")

                            with st.expander("Feature Avanzate"):
                                c1, c2, c3 = st.columns(3)
                                c1.metric("NHR", f"{result['nhr']:.4f}")
                                c2.metric("DFA", f"{result['dfa']:.4f}")
                                c3.metric("PPE", f"{result['ppe']:.4f}")
                        else:
                            st.error(error)
                else:
                    st.warning("Inserisci codice fiscale e carica audio")

    # TAB 3: Archivio
    with menu[2]:
        st.subheader("I Miei Pazienti")

        p_list = get_patients(st.session_state.user)

        if p_list:
            df_pazienti = pd.DataFrame(p_list)
            df_pazienti['sesso'] = df_pazienti['sex'].apply(lambda x: 'M' if x == 1 else 'F')

            st.dataframe(
                df_pazienti[['nome', 'cognome', 'codice_fiscale', 'age', 'sesso']],
                use_container_width=True,
                hide_index=True
            )

            pazienti_options = {
                f"{p['nome']} {p['cognome']} ({p['codice_fiscale']})": p['codice_fiscale']
                for p in p_list
            }
            sel = st.selectbox("Seleziona Paziente", list(pazienti_options.keys()))

            if sel:
                cf_selected = pazienti_options[sel]
                info, hist = get_history(cf_selected)

                if hist and len(hist) > 0:
                    df = pd.DataFrame(hist)
                    df['timestamp'] = pd.to_datetime(df['timestamp'])

                    # Grafico UPDRS
                    st.plotly_chart(create_updrs_trend_chart_medico(df), use_container_width=True)

                    # Tabella misurazioni con note
                    st.subheader("Dettaglio Misurazioni e Note")
                    for idx, row in df.iterrows():
                        with st.expander(
                                f"📅 {row['timestamp'].strftime('%d/%m/%Y %H:%M')} - UPDRS: {row['motor_updrs']:.1f}"):
                            col1, col2 = st.columns([2, 1])
                            with col1:
                                st.write(f"**Jitter:** {row['jitter']:.6f}")
                                st.write(f"**Shimmer:** {row['shimmer']:.6f}")
                            with col2:
                                st.write(f"**HNR:** {row.get('hnr', 'N/A')}")
                                st.write(f"**NHR:** {row.get('nhr', 'N/A')}")

                            # Form per aggiungere/modificare nota
                            nota_esistente = row.get('note_medico', '')

                            with st.form(f"nota_form_{idx}"):
                                nota_input = st.text_area(
                                    "Consigli per il paziente:",
                                    value=nota_esistente if nota_esistente else "",
                                    height=100,
                                    placeholder="Es: Continuare con la terapia farmacologica attuale. Consigliato aumentare l'attività fisica leggera."
                                )

                                if st.form_submit_button("💾 Salva Nota"):
                                    success, message = add_note(
                                        cf_selected,
                                        row['timestamp'].isoformat(),
                                        nota_input,
                                        st.session_state.user
                                    )
                                    if success:
                                        st.success(message)
                                        st.rerun()
                                    else:
                                        st.error(message)
                else:
                    st.info("Nessuna misurazione registrata")
        else:
            st.info("Nessun paziente registrato")

    # TAB 4: Reset Password
    with menu[3]:
        st.subheader("Reset Password Paziente")

        p_list = get_patients(st.session_state.user)

        if p_list:
            pazienti_options = {
                f"{p['nome']} {p['cognome']} ({p['codice_fiscale']})": p['codice_fiscale']
                for p in p_list
            }

            with st.form("reset_password"):
                selected_patient = st.selectbox("Seleziona Paziente", list(pazienti_options.keys()))

                col1, col2 = st.columns(2)
                with col1:
                    new_password = st.text_input("Nuova Password", type="password")
                with col2:
                    confirm_password = st.text_input("Conferma Password", type="password")

                if st.form_submit_button("Reset Password", type="primary"):
                    if not new_password or not confirm_password:
                        st.error("Compila entrambi i campi")
                    elif new_password != confirm_password:
                        st.error("Le password non corrispondono")
                    else:
                        cf_selected = pazienti_options[selected_patient]
                        success, result = reset_patient_password(
                            st.session_state.user, cf_selected, new_password
                        )

                        if success:
                            st.success(f"Password di {result} aggiornata")
                            st.info(f"Nuove credenziali:\n- CF: `{cf_selected}`\n- Password: `{new_password}`")
                        else:
                            st.error(result)
        else:
            st.info("Nessun paziente registrato")


# ==================== DASHBOARD PAZIENTE ====================

else:
    st.sidebar.title(f"{st.session_state.get('nome_completo', 'Paziente')}")
    st.sidebar.info(f"CF: {st.session_state.user}")

    if st.sidebar.button("Logout"):
        st.session_state.logged_in = False
        st.session_state.user = None
        st.session_state.selected_role = None
        st.rerun()

    st.title("Il Tuo Monitoraggio")

    # Statistiche generali
    stats = get_patient_stats(st.session_state.user)

    if stats and stats['n_misurazioni'] > 0:
        # Metriche principali
        col1, col2, col3 = st.columns(3)
        col1.metric("Numero Controlli", stats['n_misurazioni'])
        col2.metric("UPDRS Attuale", f"{stats['ultimo_updrs']:.1f}")
        col3.metric("Variazione UPDRS", f"{stats['variazione']:+.1f}")

        # Grafico principale - SOLO UPDRS
        info, data = get_history(st.session_state.user)

        if data and len(data) > 0:
            df_p = pd.DataFrame(data)
            df_p['timestamp'] = pd.to_datetime(df_p['timestamp'])

            # Grafico UPDRS semplice
            st.plotly_chart(create_updrs_trend_chart_simple(df_p), use_container_width=True)

            # Note del medico
            st.subheader("📋 Consigli del tuo Medico")

            note_presenti = False
            for idx, row in df_p.iterrows():
                if row.get('note_medico'):
                    note_presenti = True
                    with st.container():
                        st.markdown(
                            f"**{row['timestamp'].strftime('%d/%m/%Y')}** - UPDRS: {row['motor_updrs']:.1f}")
                        st.info(row['note_medico'])
                        st.markdown("---")

            if not note_presenti:
                st.info(
                    "Il tuo medico non ha ancora lasciato consigli. Verranno visualizzati qui dopo la prossima visita.")

            # Dettaglio misurazioni (opzionale)
            with st.expander("📊 Vedi dettaglio tutte le misurazioni"):
                st.dataframe(
                    df_p[['timestamp', 'motor_updrs']].rename(columns={
                        'timestamp': 'Data',
                        'motor_updrs': 'UPDRS Motorio'
                    }),
                    use_container_width=True,
                    hide_index=True
                )
    else:
        st.info("Benvenuto! Non ci sono ancora misurazioni.\n\nContatta il tuo medico per la prima visita.")