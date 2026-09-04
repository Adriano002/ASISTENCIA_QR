import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
import hashlib
import qrcode
from io import BytesIO
import os
import shutil
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
import tempfile
import zipfile
import cv2
import numpy as np

st.set_page_config(page_title="Asistencia Escolar", page_icon="🏫", layout="wide")
DB_PATH = "asistencia_enterprise.db"
BACKUP_DIR = "backups"

# ============================================================
# 1. MIGRACIÓN AUTOMÁTICA
# ============================================================

def migrar_base_datos():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA table_info(usuarios)")
        columnas = [col[1] for col in cursor.fetchall()]
        if 'turno_asignado' not in columnas:
            try: cursor.execute("ALTER TABLE usuarios ADD COLUMN turno_asignado TEXT DEFAULT 'Mañana'")
            except: pass
        if 'permisos' not in columnas:
            try: cursor.execute("ALTER TABLE usuarios ADD COLUMN permisos TEXT DEFAULT ''")
            except: pass
        
        cursor.execute("PRAGMA table_info(alumnos)")
        columnas = [col[1] for col in cursor.fetchall()]
        if 'turno' not in columnas:
            try: cursor.execute("ALTER TABLE alumnos ADD COLUMN turno TEXT DEFAULT 'Mañana'")
            except: pass
        if 'fecha_registro' not in columnas:
            try: cursor.execute("ALTER TABLE alumnos ADD COLUMN fecha_registro TEXT DEFAULT CURRENT_DATE")
            except: pass
        
        cursor.execute("PRAGMA table_info(asistencias)")
        columnas = [col[1] for col in cursor.fetchall()]
        if 'turno' not in columnas:
            try: cursor.execute("ALTER TABLE asistencias ADD COLUMN turno TEXT DEFAULT 'Mañana'")
            except: pass
        if 'metodo' not in columnas:
            try: cursor.execute("ALTER TABLE asistencias ADD COLUMN metodo TEXT DEFAULT 'manual'")
            except: pass
        if 'registrado_por' not in columnas:
            try: cursor.execute("ALTER TABLE asistencias ADD COLUMN registrado_por TEXT DEFAULT ''")
            except: pass
        
        cursor.execute("PRAGMA table_info(justificaciones)")
        columnas = [col[1] for col in cursor.fetchall()]
        if 'fecha_registro' not in columnas:
            try: cursor.execute("ALTER TABLE justificaciones ADD COLUMN fecha_registro TEXT DEFAULT CURRENT_DATE")
            except: pass
        
        conn.commit()
        conn.close()
    except:
        pass

# ============================================================
# 2. BASE DE DATOS
# ============================================================

def get_conn():
    return sqlite3.connect(DB_PATH)

def hash_pass(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            username TEXT PRIMARY KEY, password TEXT, rol TEXT, 
            seccion_asignada TEXT, turno_asignado TEXT DEFAULT 'Mañana', 
            permisos TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS alumnos (
            dni TEXT PRIMARY KEY, nombres TEXT, apellidos TEXT, 
            grado_seccion TEXT, turno TEXT DEFAULT 'Mañana', 
            fecha_registro TEXT DEFAULT CURRENT_DATE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS asistencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dni TEXT, fecha TEXT, 
            hora TEXT, estado TEXT, metodo TEXT DEFAULT 'manual', 
            registrado_por TEXT DEFAULT '', turno TEXT DEFAULT 'Mañana')""")
        c.execute("""CREATE TABLE IF NOT EXISTS justificaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dni TEXT, fecha TEXT, 
            motivo TEXT, registrado_por TEXT, 
            fecha_registro TEXT DEFAULT CURRENT_DATE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT, 
            accion TEXT, timestamp TEXT)""")
        
        if c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
            c.executemany("INSERT INTO usuarios VALUES (?,?,?,?,?,?)", [
                ("directivo1", hash_pass("dir123"), "Directivo", "TODAS", 
                 "Mañana", "puerta,ver_todo,justificaciones,admin,alumnos"),
                ("auxiliar1", hash_pass("aux123"), "Auxiliar de Puerta", 
                 "TODAS", "Mañana", "puerta"),
                ("docente_1a", hash_pass("doc123"), "Docente", "1°A", 
                 "Mañana", "justificaciones,ver_mi_aula")
            ])
        conn.commit()

init_db()
migrar_base_datos()

# ============================================================
# 3. FUNCIONES PRINCIPALES
# ============================================================

def audit(usuario, accion):
    with get_conn() as conn:
        conn.execute("INSERT INTO auditoria VALUES (NULL,?,?,?)", 
                    (usuario, accion, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

def tiene_permiso(p):
    return p in st.session_state.get('permisos', [])

def listar_alumnos(seccion=None):
    with get_conn() as conn:
        q = "SELECT * FROM alumnos"
        if seccion and seccion != "TODAS":
            q += f" WHERE grado_seccion = '{seccion}'"
        return pd.read_sql(q + " ORDER BY apellidos", conn)

def get_alumno(dni):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM alumnos WHERE dni=?", (dni,)).fetchone()

def add_alumno(dni, n, a, g, t="Mañana"):
    try:
        with get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO alumnos VALUES (?,?,?,?,?,?)", 
                        (dni,n,a,g,t,datetime.now().strftime("%Y-%m-%d")))
            conn.commit()
            return conn.total_changes > 0
    except:
        return False

def update_alumno(dni, n, a, g, t=None):
    with get_conn() as conn:
        if t:
            conn.execute("UPDATE alumnos SET nombres=?, apellidos=?, grado_seccion=?, turno=? WHERE dni=?", 
                        (n,a,g,t,dni))
        else:
            conn.execute("UPDATE alumnos SET nombres=?, apellidos=?, grado_seccion=? WHERE dni=?", 
                        (n,a,g,dni))
        conn.commit()
        return conn.total_changes > 0

def delete_alumno(dni):
    with get_conn() as conn:
        conn.execute("DELETE FROM asistencias WHERE dni=?", (dni,))
        conn.execute("DELETE FROM justificaciones WHERE dni=?", (dni,))
        conn.execute("DELETE FROM alumnos WHERE dni=?", (dni,))
        conn.commit()
        return True

def get_secciones():
    with get_conn() as conn:
        df = pd.read_sql("SELECT DISTINCT grado_seccion FROM alumnos ORDER BY grado_seccion", conn)
        return df['grado_seccion'].tolist() if not df.empty else []

def get_secciones_turno(turno):
    with get_conn() as conn:
        df = pd.read_sql("SELECT DISTINCT grado_seccion FROM alumnos WHERE turno=? ORDER BY grado_seccion", 
                        conn, params=(turno,))
        return df['grado_seccion'].tolist() if not df.empty else []

def load_excel(df):
    if df.empty:
        return 0, "Archivo vacío"
    df.columns = [c.lower().strip() for c in df.columns]
    req = ['dni','nombres','apellidos','grado_seccion']
    if not all(c in df.columns for c in req):
        return 0, "Faltan columnas: dni, nombres, apellidos, grado_seccion"
    
    tiene_turno = 'turno' in df.columns
    ok = 0
    with get_conn() as conn:
        for _, r in df.iterrows():
            try:
                dni = str(r['dni']).strip()
                n = str(r['nombres']).strip()
                a = str(r['apellidos']).strip()
                g = str(r['grado_seccion']).strip()
                if not all([dni,n,a,g]): continue
                t = 'Mañana'
                if tiene_turno and pd.notna(r['turno']):
                    t = 'Tarde' if str(r['turno']).strip().lower() in ['tarde','pm'] else 'Mañana'
                conn.execute("INSERT OR REPLACE INTO alumnos VALUES (?,?,?,?,?,?)", 
                            (dni,n,a,g,t,datetime.now().strftime("%Y-%m-%d")))
                if conn.total_changes > 0: ok += 1
            except: pass
        conn.commit()
    return ok, f"{ok} alumnos cargados"

def registrar_asistencia(dni, estado=None, metodo='manual', turno=None):
    hoy = datetime.now().strftime("%Y-%m-%d")
    hora = datetime.now().strftime("%H:%M:%S")
    if turno is None:
        with get_conn() as conn:
            r = conn.execute("SELECT turno FROM alumnos WHERE dni=?", (dni,)).fetchone()
            turno = r[0] if r else 'Mañana'
    if estado is None:
        estado = "Puntual" if hora <= "08:15:00" else "Tardanza"
    with get_conn() as conn:
        if conn.execute("SELECT id FROM asistencias WHERE dni=? AND fecha=? AND turno=?", 
                       (dni,hoy,turno)).fetchone():
            return False, f"Ya tiene asistencia en turno {turno}"
        conn.execute("""INSERT INTO asistencias 
            (dni,fecha,hora,estado,metodo,registrado_por,turno) 
            VALUES (?,?,?,?,?,?,?)""",
            (dni,hoy,hora,estado,metodo,st.session_state.user,turno))
        conn.commit()
        return True, f"Registrado como {estado}"

def get_asistencias(fecha, seccion=None):
    q = """SELECT a.*, al.nombres, al.apellidos, al.grado_seccion 
           FROM asistencias a JOIN alumnos al ON a.dni=al.dni WHERE a.fecha=?"""
    params = [fecha]
    if seccion and seccion != "TODAS LAS SECCIONES":
        q += " AND al.grado_seccion=?"
        params.append(seccion)
    with get_conn() as conn:
        return pd.read_sql(q + " ORDER BY a.hora DESC", conn, params=params)

def delete_asistencia(id):
    with get_conn() as conn:
        conn.execute("DELETE FROM asistencias WHERE id=?", (id,))
        conn.commit()
        return conn.total_changes > 0

def add_just(dni, fecha, motivo):
    with get_conn() as conn:
        conn.execute("INSERT INTO justificaciones (dni,fecha,motivo,registrado_por) VALUES (?,?,?,?)",
                    (dni,str(fecha),motivo,st.session_state.user))
        conn.commit()
        return True

def get_just():
    with get_conn() as conn:
        return pd.read_sql("""SELECT j.*, a.apellidos, a.nombres, a.grado_seccion 
            FROM justificaciones j JOIN alumnos a ON j.dni=a.dni 
            ORDER BY j.id DESC""", conn)

def delete_just(id):
    with get_conn() as conn:
        conn.execute("DELETE FROM justificaciones WHERE id=?", (id,))
        conn.commit()
        return conn.total_changes > 0

def get_users():
    with get_conn() as conn:
        return pd.read_sql("SELECT username,rol,seccion_asignada,turno_asignado,permisos FROM usuarios", conn)

def add_user(username, password, rol, seccion, turno, permisos):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO usuarios VALUES (?,?,?,?,?,?)",
                    (username,hash_pass(password),rol,seccion,turno,permisos))
        conn.commit()
        return conn.total_changes > 0

def update_user(username, rol, seccion, turno, permisos):
    with get_conn() as conn:
        conn.execute("UPDATE usuarios SET rol=?, seccion_asignada=?, turno_asignado=?, permisos=? WHERE username=?",
                    (rol,seccion,turno,permisos,username))
        conn.commit()
        return conn.total_changes > 0

def delete_user(username):
    if username in ['directivo1','auxiliar1']:
        return False, "No se puede eliminar"
    with get_conn() as conn:
        conn.execute("DELETE FROM usuarios WHERE username=?", (username,))
        conn.commit()
        return conn.total_changes > 0, "Eliminado"

def change_pass(username, new_pass):
    with get_conn() as conn:
        conn.execute("UPDATE usuarios SET password=? WHERE username=?", (hash_pass(new_pass), username))
        conn.commit()
        return conn.total_changes > 0

def get_turno_user(username):
    with get_conn() as conn:
        r = conn.execute("SELECT turno_asignado FROM usuarios WHERE username=?", (username,)).fetchone()
        return r[0] if r else 'Mañana'

def update_turno_user(username, turno):
    with get_conn() as conn:
        conn.execute("UPDATE usuarios SET turno_asignado=? WHERE username=?", (turno, username))
        conn.commit()
        return conn.total_changes > 0

def get_secciones_user(username):
    with get_conn() as conn:
        r = conn.execute("SELECT seccion_asignada, turno_asignado FROM usuarios WHERE username=?", (username,)).fetchone()
        if not r: return []
        if r[0] == 'TODAS':
            return get_secciones_turno(r[1])
        secs = [s.strip() for s in r[0].split(',') if s.strip()]
        if not secs: return []
        with get_conn() as conn2:
            q = f"SELECT DISTINCT grado_seccion FROM alumnos WHERE grado_seccion IN ({','.join(['?']*len(secs))}) AND turno=?"
            df = pd.read_sql(q, conn2, params=secs+[r[1]])
            return df['grado_seccion'].tolist() if not df.empty else []

def resumen_alumno(dni):
    with get_conn() as conn:
        a = conn.execute("SELECT COUNT(*) FROM asistencias WHERE dni=? AND estado IN ('Puntual','Tardanza')", (dni,)).fetchone()[0]
        t = conn.execute("SELECT COUNT(*) FROM asistencias WHERE dni=? AND estado='Tardanza'", (dni,)).fetchone()[0]
        f = conn.execute("SELECT COUNT(*) FROM asistencias WHERE dni=? AND estado='Falta'", (dni,)).fetchone()[0]
        j = conn.execute("SELECT COUNT(*) FROM justificaciones WHERE dni=?", (dni,)).fetchone()[0]
        return {'asistencias':a, 'tardanzas':t, 'faltas':f, 'justificaciones':j, 'faltas_efectivas':max(0,f-j)}

# ============================================================
# 4. QR Y DETECCIÓN
# ============================================================

def generar_pdf_qr(seccion, df_alumnos):
    if df_alumnos.empty:
        return None, "No hay alumnos"
    
    pdf_path = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf').name
    c = canvas.Canvas(pdf_path, pagesize=landscape(A4))
    width, height = landscape(A4)
    
    cols = 4
    rows = 3
    qr_size = 60 * mm
    spacing_x = (width - (cols * qr_size)) / (cols + 1)
    spacing_y = (height - (rows * qr_size)) / (rows + 1)
    
    x_pos = spacing_x
    y_pos = height - spacing_y - qr_size
    count = 0
    
    for _, row in df_alumnos.iterrows():
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(row['dni'])
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white")
        
        qr_temp = tempfile.NamedTemporaryFile(delete=False, suffix='.png').name
        img_qr.save(qr_temp, "PNG")
        
        c.drawImage(qr_temp, x_pos, y_pos, width=qr_size, height=qr_size)
        c.setFont("Helvetica", 10)
        nombre_completo = f"{row['apellidos']}, {row['nombres']}"
        c.drawCentredString(x_pos + qr_size/2, y_pos - 15, nombre_completo[:30])
        c.setFont("Helvetica", 8)
        c.drawCentredString(x_pos + qr_size/2, y_pos - 28, f"DNI: {row['dni']}")
        c.drawCentredString(x_pos + qr_size/2, y_pos - 40, row['grado_seccion'])
        
        count += 1
        x_pos += qr_size + spacing_x
        if count % cols == 0:
            x_pos = spacing_x
            y_pos -= qr_size + spacing_y
        if count % (cols * rows) == 0 and count < len(df_alumnos):
            c.showPage()
            x_pos = spacing_x
            y_pos = height - spacing_y - qr_size
    
    c.save()
    return pdf_path, f"PDF generado con {len(df_alumnos)} QR"

def generar_zip_qr(seccion, df_alumnos):
    if df_alumnos.empty:
        return None, "No hay alumnos"
    
    zip_path = tempfile.NamedTemporaryFile(delete=False, suffix='.zip').name
    
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for _, row in df_alumnos.iterrows():
            qr = qrcode.QRCode(version=1, box_size=6, border=2)
            qr.add_data(row['dni'])
            qr.make(fit=True)
            img_qr = qr.make_image(fill_color="black", back_color="white")
            
            img_buffer = BytesIO()
            img_qr.save(img_buffer, format="PNG")
            img_buffer.seek(0)
            
            nombre_archivo = f"{row['apellidos']}_{row['nombres']}_{row['dni']}.png".replace(" ", "_")
            zipf.writestr(nombre_archivo, img_buffer.getvalue())
    
    return zip_path, f"ZIP generado con {len(df_alumnos)} QR"

def procesar_qr(imagen_bytes):
    try:
        nparr = np.frombuffer(imagen_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        detector = cv2.QRCodeDetector()
        data, bbox, _ = detector.detectAndDecode(img)
        if data:
            return data.strip(), None
        return None, "No se detectó ningún código QR"
    except Exception as e:
        return None, f"Error al leer: {e}"

# ============================================================
# 5. CÁMARA (ST.CAMERA_INPUT)
# ============================================================

def mostrar_camara_vivo():
    if 'dni_qr_foto' not in st.session_state:
        st.session_state.dni_qr_foto = None
    
    st.info("📸 Toma una foto del QR. En celular: toca el ícono 🔄 para cambiar a cámara trasera")
    
    imagen = st.camera_input("📸 Tomar foto", key="camara_qr")
    
    if imagen is not None:
        st.image(imagen, width=200)
        if st.button("🔍 Procesar QR", use_container_width=True, type="primary"):
            with st.spinner("Procesando..."):
                dni, error = procesar_qr(imagen.getvalue())
            if error:
                st.warning(f"⚠️ {error}")
            elif dni:
                st.session_state.dni_qr_foto = dni
                st.success(f"✅ QR detectado: {dni}")
                st.rerun()
            else:
                st.error("❌ No se detectó QR")
    
    if st.session_state.dni_qr_foto:
        dni = st.session_state.dni_qr_foto
        alumno = get_alumno(dni)
        if alumno:
            st.success(f"✅ QR detectado: {dni}")
            st.info(f"👤 {alumno[2]}, {alumno[1]} - {alumno[3]}")
            
            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("✅ Puntual", key="cam_p", use_container_width=True):
                    ok, msg = registrar_asistencia(dni, "Puntual", "qr_cam")
                    if ok:
                        audit(st.session_state.user, f"QR Cam {dni}")
                        st.success(f"✔ {msg}")
                        st.balloons()
                        st.session_state.dni_qr_foto = None
                        st.rerun()
                    else:
                        st.warning(f"⚠️ {msg}")
            with col2:
                if st.button("🟡 Tardanza", key="cam_t", use_container_width=True):
                    ok, msg = registrar_asistencia(dni, "Tardanza", "qr_cam")
                    if ok:
                        audit(st.session_state.user, f"QR Cam Tardanza {dni}")
                        st.warning(f"⚠️ {msg}")
                        st.session_state.dni_qr_foto = None
                        st.rerun()
                    else:
                        st.warning(f"⚠️ {msg}")
            with col3:
                if st.button("🔴 Falta", key="cam_f", use_container_width=True):
                    ok, msg = registrar_asistencia(dni, "Falta", "qr_cam")
                    if ok:
                        audit(st.session_state.user, f"QR Cam Falta {dni}")
                        st.error(f"❌ {msg}")
                        st.session_state.dni_qr_foto = None
                        st.rerun()
                    else:
                        st.warning(f"⚠️ {msg}")

# ============================================================
# 6. SESIÓN Y LOGIN
# ============================================================

if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.user = None
    st.session_state.rol = None
    st.session_state.permisos = []

if not st.session_state.logged_in:
    st.title("🏫 Control de Asistencia")
    with st.form("login_form"):
        u = st.text_input("Usuario")
        p = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Acceder"):
            with get_conn() as conn:
                r = conn.execute("""SELECT rol, seccion_asignada, turno_asignado, permisos 
                    FROM usuarios WHERE username=? AND password=?""", 
                    (u, hash_pass(p))).fetchone()
            if r:
                st.session_state.logged_in = True
                st.session_state.user = u
                st.session_state.rol = r[0]
                st.session_state.seccion = r[1]
                st.session_state.turno = r[2] if r[2] else "Mañana"
                st.session_state.permisos = r[3].split(',') if r[3] else []
                audit(u, "Login")
                st.rerun()
            else:
                st.error("Credenciales inválidas")
    st.stop()

# ============================================================
# 7. SIDEBAR
# ============================================================

with st.sidebar:
    st.write(f"**👤 {st.session_state.user}**")
    st.write(f"**🎯 {st.session_state.rol}**")
    st.write(f"**🕐 {st.session_state.get('turno','Mañana')}**")
    st.divider()
    if st.button("🚪 Cerrar Sesión"):
        audit(st.session_state.user, "Logout")
        st.session_state.logged_in = False
        st.rerun()

# ============================================================
# 8. CONTENIDO PRINCIPAL
# ============================================================

st.title("📚 Sistema de Asistencia")

with get_conn() as conn:
    total = pd.read_sql("SELECT COUNT(*) as c FROM alumnos", conn)['c'][0]
    hoy = pd.read_sql("SELECT COUNT(*) as c FROM asistencias WHERE fecha=?", 
                     conn, params=(datetime.now().strftime("%Y-%m-%d"),))['c'][0]
    total_asistencias = pd.read_sql("SELECT COUNT(*) as c FROM asistencias", conn)['c'][0]

col1, col2, col3 = st.columns(3)
col1.metric("👥 Alumnos", total)
col2.metric("✅ Asistencias Hoy", hoy)
col3.metric("📊 Total Asistencias", total_asistencias)
st.divider()

# ============================================================
# 9. TABS
# ============================================================

if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    tabs = st.tabs([
        "🚪 Registrar", "👥 Alumnos", "📋 Semáforo", "📊 Reportes", 
        "📝 Justificaciones", "🖨️ Carnets", "💾 Backup", "⚙️ Admin"
    ])
else:
    tabs = st.tabs(["📋 Mi Aula", "📊 Reportes", "📝 Justificaciones"])

# ============================================================
# 10. REGISTRAR
# ============================================================

if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    with tabs[0]:
        if not tiene_permiso("puerta"):
            st.error("Sin permiso")
        else:
            st.subheader("🚪 Registrar Asistencia")
            
            modo = st.radio(
                "Método:", 
                ["📷 Tomar foto QR", "📋 Selección Manual"], 
                horizontal=True, 
                key="modo_registro"
            )
            
            if modo == "📷 Tomar foto QR":
                mostrar_camara_vivo()
            else:
                st.info("📋 Seleccione la sección y el alumno")
                
                secs = get_secciones_user(st.session_state.user) or []
                if not secs:
                    st.warning("Sin secciones asignadas")
                else:
                    sec = st.selectbox("Sección:", secs, key="sec_registro")
                    df = listar_alumnos(sec)
                    
                    if df.empty:
                        st.info("No hay alumnos")
                    else:
                        df['display'] = df['apellidos'] + ", " + df['nombres'] + " (DNI: " + df['dni'] + ")"
                        alumno = st.selectbox("Alumno:", df['display'].tolist(), key="alumno_registro")
                        
                        if alumno:
                            dni = alumno.split("(DNI: ")[-1].replace(")", "").strip()
                            alumno_data = get_alumno(dni)
                            if alumno_data:
                                st.info(f"👤 {alumno_data[2]}, {alumno_data[1]} - {alumno_data[3]}")
                            
                            col1, col2, col3 = st.columns(3)
                            with col1:
                                if st.button("✅ Puntual", key="man_p", use_container_width=True):
                                    ok, msg = registrar_asistencia(dni, "Puntual", "manual")
                                    if ok:
                                        audit(st.session_state.user, f"Asistencia Manual {dni}")
                                        st.success(f"✔ {msg}")
                                        st.rerun()
                                    else:
                                        st.warning(f"⚠️ {msg}")
                            with col2:
                                if st.button("🟡 Tardanza", key="man_t", use_container_width=True):
                                    ok, msg = registrar_asistencia(dni, "Tardanza", "manual")
                                    if ok:
                                        audit(st.session_state.user, f"Tardanza Manual {dni}")
                                        st.warning(f"⚠️ {msg}")
                                        st.rerun()
                                    else:
                                        st.warning(f"⚠️ {msg}")
                            with col3:
                                if st.button("🔴 Falta", key="man_f", use_container_width=True):
                                    ok, msg = registrar_asistencia(dni, "Falta", "manual")
                                    if ok:
                                        audit(st.session_state.user, f"Falta Manual {dni}")
                                        st.error(f"❌ {msg}")
                                        st.rerun()
                                    else:
                                        st.warning(f"⚠️ {msg}")

# ============================================================
# 11. ALUMNOS
# ============================================================

if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    with tabs[1]:
        if not tiene_permiso("alumnos"):
            st.error("Sin permiso")
        else:
            st.subheader("👥 Gestión de Alumnos")
            tabs2 = st.tabs(["📋 Lista", "➕ Agregar", "✏️ Editar", "🗑️ Eliminar", "📤 Carga Masiva", "🔄 Turnos"])
            
            with tabs2[0]:
                sec = st.selectbox("Filtrar:", ["TODAS"] + get_secciones(), key="filtro_lista")
                df = listar_alumnos(sec)
                if df.empty: st.info("Sin alumnos")
                else: st.dataframe(df)
            
            with tabs2[1]:
                with st.form("add_alumno_form"):
                    c1,c2 = st.columns(2)
                    with c1:
                        dni = st.text_input("DNI*")
                        n = st.text_input("Nombres*")
                    with c2:
                        a = st.text_input("Apellidos*")
                        g = st.text_input("Grado*")
                        t = st.selectbox("Turno:", ["Mañana","Tarde"])
                    if st.form_submit_button("Agregar"):
                        if all([dni,n,a,g]):
                            if add_alumno(dni,n,a,g,t):
                                audit(st.session_state.user, f"Agregó {dni}")
                                st.success("✅ Agregado"); st.rerun()
                            else: st.error("DNI ya existe")
                        else: st.error("Todos los campos son obligatorios")
            
            with tabs2[2]:
                sec = st.selectbox("Sección:", get_secciones(), key="edit_sec")
                df = listar_alumnos(sec)
                if not df.empty:
                    df['display'] = df['apellidos'] + ", " + df['nombres']
                    alum = st.selectbox("Alumno:", df['display'].tolist(), key="edit_alum")
                    if alum:
                        row = df[df['display']==alum].iloc[0]
                        with st.form("edit_alumno_form"):
                            c1,c2 = st.columns(2)
                            with c1:
                                n_e = st.text_input("Nombres", value=row['nombres'])
                            with c2:
                                a_e = st.text_input("Apellidos", value=row['apellidos'])
                            g_e = st.text_input("Grado", value=row['grado_seccion'])
                            t_e = st.selectbox("Turno:", ["Mañana","Tarde"], 
                                              index=0 if row.get('turno','Mañana')=='Mañana' else 1)
                            if st.form_submit_button("Actualizar"):
                                if update_alumno(row['dni'], n_e, a_e, g_e, t_e):
                                    audit(st.session_state.user, f"Actualizó {row['dni']}")
                                    st.success("✅ Actualizado"); st.rerun()
                                else: st.error("Error")
            
            with tabs2[3]:
                sec = st.selectbox("Sección:", get_secciones(), key="del_sec")
                df = listar_alumnos(sec)
                if not df.empty:
                    df['display'] = df['apellidos'] + ", " + df['nombres']
                    alum = st.selectbox("Alumno:", df['display'].tolist(), key="del_alum")
                    if alum:
                        dni = df[df['display']==alum]['dni'].iloc[0]
                        st.warning(f"¿Eliminar {alum}?")
                        if st.button("🗑️ Eliminar", key="btn_del_alum"):
                            if delete_alumno(dni):
                                audit(st.session_state.user, f"Eliminó {dni}")
                                st.success("✅ Eliminado"); st.rerun()
            
            with tabs2[4]:
                arch = st.file_uploader("Excel/CSV:", type=['xlsx','csv'], key="upload_excel")
                if arch:
                    try:
                        df = pd.read_csv(arch) if arch.name.endswith('.csv') else pd.read_excel(arch)
                        st.dataframe(df.head(5))
                        if st.button("Cargar", key="btn_cargar_excel"):
                            ok, msg = load_excel(df)
                            st.success(msg)
                            if ok > 0: audit(st.session_state.user, f"Carga masiva: {ok} alumnos"); st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
            
            with tabs2[5]:
                st.subheader("🔄 Cambiar Turno de Sección")
                with get_conn() as conn:
                    df_t = pd.read_sql("SELECT grado_seccion, turno FROM alumnos GROUP BY grado_seccion, turno ORDER BY grado_seccion", conn)
                if not df_t.empty:
                    st.dataframe(df_t)
                    sec = st.selectbox("Sección:", df_t['grado_seccion'].unique().tolist(), key="sec_turno")
                    if sec:
                        t_actual = df_t[df_t['grado_seccion']==sec]['turno'].iloc[0]
                        t_nuevo = "Tarde" if t_actual=="Mañana" else "Mañana"
                        if st.button(f"Cambiar a {t_nuevo}", key="btn_cambiar_turno_sec"):
                            with get_conn() as conn3:
                                conn3.execute("UPDATE alumnos SET turno=? WHERE grado_seccion=?", (t_nuevo, sec))
                                conn3.commit()
                            audit(st.session_state.user, f"Turno {sec} a {t_nuevo}")
                            st.success("✅ Actualizado"); st.rerun()

# ============================================================
# 12. SEMÁFORO
# ============================================================

idx_sema = 2 if st.session_state.rol in ["
