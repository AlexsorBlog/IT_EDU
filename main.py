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

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

app = Flask(__name__)
app.secret_key = "your_secret_key"

# OpenAI API Key
openai.api_key = "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"

# Google API Configuration
CLIENT_SECRETS_FILE = "credentials_google.json"
SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.student-submissions.me.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me.readonly'
]

@app.template_filter('markdown')
def markdown_filter(text):
    return Markup(markdown.markdown(text))

def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

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

@app.route('/callback')
def oauth2callback():
    state = session['state']
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, SCOPES, state=state)
    flow.redirect_uri = url_for('oauth2callback', _external=True)
    flow.fetch_token(authorization_response=request.url)
    credentials = flow.credentials
    session['credentials'] = credentials_to_dict(credentials)
    return redirect(url_for('index'))

def get_credentials():
    if 'credentials' not in session:
        return None
    creds = google.oauth2.credentials.Credentials(**session['credentials'])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        session['credentials'] = credentials_to_dict(creds)
    return creds

@app.route("/", methods=["GET", "POST"])
def index():
    if "chat_history" not in session:
        session["chat_history"] = []
    if "tasks" not in session:
        session["tasks"] = []

    assistant_reply = None
    current_date = datetime(2025, 3, 13)  # Replace with datetime.now() for real-time
    today = current_date.date()
    tomorrow = today + timedelta(days=1)

    # Fetch tasks from Google Classroom if not already in session
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
                        # Fetch student submission to check state and grade
                        submissions = service.courses().courseWork().studentSubmissions().list(
                            courseId=course_id,
                            courseWorkId=assignment['id']
                        ).execute()
                        submission = submissions.get('studentSubmissions', [{}])[0]  # Get first submission (assuming user’s)
                        submission_state = submission.get('state', 'NEW')
                        has_grade = 'assignedGrade' in submission or 'draftGrade' in submission

                        # Include only tasks due today/tomorrow or overdue and not completed
                        if (due == today or due == tomorrow or (due < today and submission_state != 'TURNED_IN' and not has_grade)):
                            tasks.append({
                                'course': course_name,
                                'title': assignment['title'],
                                'due': due.strftime('%Y-%m-%d'),
                                'state': 'Пропущено' if due < today else 'Очікує'
                            })

            session["tasks"] = tasks
            session["tasks_fetched"] = True
            session.modified = True
        except googleapiclient.errors.HttpError as error:
            session["tasks"] = [{'course': 'Помилка', 'title': f"Виникла помилка: {error}", 'due': '', 'state': ''}]
            session.modified = True

    # Handle POST requests for chat
    if request.method == "POST":
        if "chat" in request.form:
            question = request.form.get("question")
            session["chat_history"].append({"role": "user", "content": question})
            full_context = [
                {"role": "system", "content": "Ти – AI-помічник у навчанні. Не давай готових відповідей, лише допомагай користувачу вчитись самостійно через пояснення, матеріали, ресурси, ідеї та підтримку. Відповідай коротко і просто"},
                *session["chat_history"]
            ]
            try:
                completion = openai.ChatCompletion.create(
                    model="gpt-4o-mini",
                    messages=full_context
                )
                reply = completion.choices[0].message.content.strip()
                session["chat_history"].append({"role": "assistant", "content": reply})
                assistant_reply = reply
                session.modified = True
            except Exception as e:
                assistant_reply = f"Сталася помилка: {str(e)}"
        else:
            topic = request.form.get("topic")
            level = request.form.get("level")
            session["topic"] = topic
            session["level"] = level
            intro_prompt = (
                f"Користувач хоче вивчити тему '{topic}', рівень знань – {level}. Допоможи скласти навчальний план, ресурси, поясни з чого почати, але не давай готових рішень – лише підтримку."
            )
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
        logged_in=credentials is not None
    )

@app.route("/remove_task/<int:task_index>", methods=["POST"])
def remove_task(task_index):
    if "tasks" in session and 0 <= task_index < len(session["tasks"]):
        session["tasks"].pop(task_index)
        session.modified = True
    return redirect(url_for("index"))

@app.route("/complete_task/<int:task_index>", methods=["POST"])
def complete_task(task_index):
    if "tasks" in session and 0 <= task_index < len(session["tasks"]):
        session["tasks"][task_index]["state"] = "Виконано"
        session.modified = True
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)

#gpt-4o-mini
#openai.api_key = "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"
