from flask import Flask, request, render_template, session, redirect, url_for
import markdown
from markupsafe import Markup
import openai
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.errors
from google.auth.transport.requests import Request
import google.oauth2.credentials
import os
from datetime import datetime, timedelta
import sqlite3
from googleapiclient.discovery import build
import logging
import requests
import PyPDF2
from io import BytesIO
from docx import Document

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = "your_secret_key"  # Replace with a secure key

with open("openai_key.txt", "r") as file:
    openai.api_key = file.readline()

# Google API Configuration
CLIENT_SECRETS_FILE = "credentials_google.json"
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.student-submissions.me.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.students.readonly',
    'https://www.googleapis.com/auth/drive.readonly'
]

# Allow insecure transport for local testing (remove in production)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

# Database initialization
def init_db():
    with sqlite3.connect("users.db") as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT,
                token TEXT,
                refresh_token TEXT,
                token_uri TEXT,
                client_id TEXT,
                client_secret TEXT,
                scopes TEXT
            )
        """)
        c.execute("DROP TABLE IF EXISTS chat_history")
        c.execute("""
            CREATE TABLE chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        conn.commit()

init_db()

# Save user credentials to database
def save_user(user_id, email, credentials):
    with sqlite3.connect("users.db") as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO users 
            (user_id, email, token, refresh_token, token_uri, client_id, client_secret, scopes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, email,
            credentials.token,
            credentials.refresh_token,
            credentials.token_uri,
            credentials.client_id,
            credentials.client_secret,
            ','.join(credentials.scopes)
        ))
        conn.commit()

# Load credentials from database
def load_credentials_from_db(user_id):
    with sqlite3.connect("users.db") as conn:
        c = conn.cursor()
        c.execute("SELECT token, refresh_token, token_uri, client_id, client_secret, scopes FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row:
            return google.oauth2.credentials.Credentials(
                token=row[0],
                refresh_token=row[1],
                token_uri=row[2],
                client_id=row[3],
                client_secret=row[4],
                scopes=row[5].split(",")
            )
    return None

# Get or refresh credentials
def get_credentials():
    user_id = session.get('user_id')
    if not user_id:
        return None
    creds = load_credentials_from_db(user_id)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_user(user_id, session.get('email'), creds)
    return creds

# Load chat history from database
def load_chat_history(user_id):
    with sqlite3.connect("users.db") as conn:
        c = conn.cursor()
        c.execute("  SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY timestamp", (user_id,))
        return [{"role": row[0], "content": row[1]} for row in c.fetchall()]

# Save chat message to database
def save_chat_message(user_id, role, content):
    with sqlite3.connect("users.db") as conn:
        c = conn.cursor()
        c.execute("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
        conn.commit()

# Markdown filter for templates
@app.template_filter('markdown')
def markdown_filter(text):
    return Markup(markdown.markdown(text))

# Login route
@app.route('/login')
def login():
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, SCOPES)
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    session['state'] = state
    return redirect(authorization_url)

# OAuth callback route
@app.route('/callback')
def oauth2callback():
    state = session.get('state')
    if not state:
        return redirect(url_for('index'))

    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, SCOPES, state=state)
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    flow.fetch_token(authorization_response=request.url)
    credentials = flow.credentials

    logging.debug(f"Credentials: token={credentials.token}, scopes={credentials.scopes}")

    granted_scopes = set(credentials.scopes)
    required_scopes = set(SCOPES)
    if not required_scopes.issubset(granted_scopes):
        missing_scopes = required_scopes - granted_scopes
        return f"Error: Missing required scopes: {missing_scopes}. Please re-authorize and grant all permissions.", 403

    try:
        oauth2_service = build('oauth2', 'v2', credentials=credentials)
        user_info = oauth2_service.userinfo().get().execute()
        user_id = user_info['id']
        email = user_info['email']
    except Exception as e:
        return f"Error getting user info: {e}", 500

    save_user(user_id, email, credentials)
    session['user_id'] = user_id
    session['email'] = email
    session['logged_in'] = True
    return redirect(url_for('index'))

# Fetch task details including file content
def get_task_details(credentials, course_id, coursework_id):
    service = googleapiclient.discovery.build('classroom', 'v1', credentials=credentials)
    drive_service = googleapiclient.discovery.build('drive', 'v3', credentials=credentials)
    coursework = service.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()
    materials = coursework.get('materials', [])
    description = coursework.get('description', '')
    title = coursework.get('title', '')
    task_context = f"Task: {title}\nDescription: {description}\nMaterials: "

    for material in materials:
        if 'link' in material:
            task_context += f"Link: {material['link']['url']}, "
        elif 'driveFile' in material:
            file_id = material['driveFile']['driveFile']['id']
            try:
                file_metadata = drive_service.files().get(fileId=file_id, fields='mimeType').execute()
                mime_type = file_metadata.get('mimeType', 'Unknown MIME type')
                content = ""

                print(f"Fetching file: {file_id}, MIME type: {mime_type}")

                if mime_type == 'application/vnd.google-apps.document':
                    # Google Docs: Export as plain text
                    content = drive_service.files().export(fileId=file_id, mimeType='text/plain').execute().decode('utf-8')
                elif mime_type == 'application/pdf':
                    # PDFs: Download and extract text
                    request = drive_service.files().get_media(fileId=file_id)
                    file_data = request.execute()
                    pdf_file = BytesIO(file_data)
                    pdf_reader = PyPDF2.PdfReader(pdf_file)
                    content = ""
                    for page in pdf_reader.pages:
                        content += page.extract_text() or ""
                elif mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                    # .docx: Download and extract text
                    request = drive_service.files().get_media(fileId=file_id)
                    file_data = request.execute()
                    docx_file = BytesIO(file_data)
                    doc = Document(docx_file)
                    content = "\n".join([para.text for para in doc.paragraphs])
                else:
                    content = f"Unsupported file type: {mime_type}"

                # Print file content to console for debugging
                print(f"File Content for {file_id} ({mime_type}): {content}")

                # Truncate content to 4000 characters
                if len(content) > 4000:
                    content = content[:4000] + " [Content truncated due to length]"
                task_context += f"File Content: {content}, "
            except Exception as e:
                print(f"Error fetching file {file_id}: {str(e)}")
                task_context += f"File Error: {str(e)}, "

    return task_context.strip(', ')

# Main route
@app.route("/", methods=["GET", "POST"])
def index():
    user_id = session.get('user_id')
    if not user_id and 'logged_in' in session:
        return redirect(url_for('login'))

    if user_id:
        if "chat_history" not in session:
            session["chat_history"] = load_chat_history(user_id)
    else:
        if "chat_history" not in session:
            session["chat_history"] = []

    if "tasks" not in session:
        session["tasks"] = []

    assistant_reply = None
    current_date = datetime.now()
    today = current_date.date()
    tomorrow = today + timedelta(days=1)

    credentials = get_credentials()
    if credentials and not session.get("tasks_fetched", False):
        try:
            service = googleapiclient.discovery.build('classroom', 'v1', credentials=credentials)
            courses_data = service.courses().list().execute()
            courses = courses_data.get('courses', [])
            tasks = []

            for course in courses:
                course_id = course['id']
                course_name = course['name']
                coursework_data = service.courses().courseWork().list(courseId=course_id).execute()
                assignments = coursework_data.get('courseWork', [])

                for assignment in assignments:
                    due_date = assignment.get('dueDate')
                    if due_date:
                        due = datetime(
                            due_date.get('year'),
                            due_date.get('month'),
                            due_date.get('day')
                        ).date()
                        submissions = service.courses().courseWork().studentSubmissions().list(
                            courseId=course_id,
                            courseWorkId=assignment['id']
                        ).execute()
                        submission = submissions.get('studentSubmissions', [{}])[0]
                        submission_state = submission.get('state', 'NEW')
                        has_grade = 'assignedGrade' in submission or 'draftGrade' in submission

                        if (due == today or due == tomorrow or (due < today and submission_state != 'TURNED_IN' and not has_grade)):
                            tasks.append({
                                'course': course_name,
                                'title': assignment['title'],
                                'due': due.strftime('%Y-%m-%d'),
                                'state': 'Пропущено' if due < today else 'Очікує',
                                'course_id': course_id,
                                'coursework_id': assignment['id']
                            })

            session["tasks"] = tasks
            session["tasks_fetched"] = True
            session.modified = True
        except googleapiclient.errors.HttpError as error:
            session["tasks"] = [{'course': 'Помилка', 'title': f"Виникла помилка: {error}", 'due': '', 'state': ''}]
            session.modified = True

    selected_task_index = session.get('selected_task_index')
    task_context = None
    if selected_task_index is not None and 0 <= selected_task_index < len(session["tasks"]):
        print(session["tasks"])
        task = session["tasks"][selected_task_index]
        try:
            print(task)
            task_context = get_task_details(credentials, task['course_id'], task['coursework_id'])
        except Exception as e:
            task_context = f"Error fetching task details: {str(e)}"

    if request.method == "POST":
        if "chat" in request.form:
            question = request.form.get("question")
            if user_id:
                save_chat_message(user_id, "user", question)
            session["chat_history"].append({"role": "user", "content": question})
            system_prompt = (
                "Ти – AI-помічник у навчанні. Твоя мета – допомагати користувачу зрозуміти завдання і вчитися самостійно. "
                "Не давай повних розв’язків чи готових відповідей "
                "Замість цього надавай підказки, пояснення, приклади підходів, проте можеш показувати приклади розв'язку чи пояснень"
            )
            if task_context:
                print(task_context)
                system_prompt += (
                    "Використовуй контекст лише для надання релевантних підказок, не розв’язуй завдання за користувача." + f"\nКонтекст завдання:\n{task_context}\n(це завдання з класруму студента котрий задає питання)"
                )
            full_context = [
                {"role": "system", "content": system_prompt},
                *session["chat_history"]
            ]
            try:
                completion = openai.ChatCompletion.create(
                    model="gpt-4o-mini",
                    messages=full_context
                )
                reply = completion.choices[0].message.content.strip()
                if user_id:
                    save_chat_message(user_id, "assistant", reply)
                session["chat_history"].append({"role": "assistant", "content": reply})
                assistant_reply = reply
                session.modified = True
            except Exception as e:
                assistant_reply = f"Сталася помилка: {str(e)}"
        elif "select_task" in request.form:
            selected_task_index = int(request.form.get("task_index"))
            session['selected_task_index'] = selected_task_index
            session.modified = True
        elif "deselect_task" in request.form:  # New action to deselect
            session['selected_task_index'] = None
            session.modified = True
        else:
            topic = request.form.get("topic")
            level = request.form.get("level")
            session["topic"] = topic
            session["level"] = level
            intro_prompt = (
                f"Користувач хоче вивчити тему '{topic}', рівень знань – {level}. Допоможи скласти навчальний план, ресурси, поясни з чого почати, але не давай готових рішень – лише підтримку."
            )
            if user_id:
                save_chat_message(user_id, "user", intro_prompt)
            session["chat_history"] = [{"role": "user", "content": intro_prompt}]
            session["started_chat"] = True
            try:
                completion = openai.ChatCompletion.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Ти – AI-помічник у навчанні. Допомагай, але не давай готових рішень. Стимулюй самостійне мислення."},
                        {"role": "user", "content": intro_prompt}
                    ]
                )
                reply = completion.choices[0].message.content.strip()
                if user_id:
                    save_chat_message(user_id, "assistant", reply)
                session["chat_history"].append({"role": "assistant", "content": reply})
                assistant_reply = reply
                session.modified = True
            except Exception as e:
                assistant_reply = f"Сталася помилка: {str(e)}"

    return render_template(
        "index.html",
        topic=session.get("topic"),
        level=session.get("level", "початківець"),
        chat_history=session.get("chat_history", []),
        started_chat=session.get("started_chat", False),
        assistant_reply=assistant_reply,
        tasks=session.get("tasks", []),
        logged_in=credentials is not None,
        selected_task_index=selected_task_index
    )

# Remove task
@app.route("/remove_task/<int:task_index>", methods=["POST"])
def remove_task(task_index):
    if "tasks" in session and 0 <= task_index < len(session["tasks"]):
        session["tasks"].pop(task_index)
        if session.get('selected_task_index') == task_index:
            session.pop('selected_task_index')
        session.modified = True
    return redirect(url_for("index"))

# Complete task
@app.route("/complete_task/<int:task_index>", methods=["POST"])
def complete_task(task_index):
    if "tasks" in session and 0 <= task_index < len(session["tasks"]):
        session["tasks"][task_index]["state"] = "Виконано"
        session.modified = True
    return redirect(url_for("index"))

# Logout route
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)

#os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
#os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'