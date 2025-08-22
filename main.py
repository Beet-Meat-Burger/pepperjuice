import string
import time
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random
from pocketbase import PocketBase  # Client also works the same
import os
from dotenv import load_dotenv

print(load_dotenv("environment.env", verbose=True, override=True))


client = PocketBase('https://67e2174q4tpcvud.pocketbasecloud.com/')
user_data = client.collection("users").auth_with_password(os.environ.get('PB_EMAIL'), os.environ.get('PB_PASS'))
adminCode = os.environ.get('ADMIN_CODE', "secret823985")
print(adminCode)
teamIdIncrement = 300
acceptingResponses = False
currentQuestionIndex = 0
users = {}

app = Flask(
    __name__,
    template_folder = "templates",
    static_folder = "static"
)
socketio = SocketIO(app)

def getBowlJson():
    return client.collection("data").get_one(os.environ.get('PB_ID')).field

bowlJson = getBowlJson()

def generateRandomLetters(length):
    generated = set()
    charset = string.ascii_uppercase
    def generate():
        max_attempts = 10000
        for _ in range(max_attempts):
            candidate = ''.join(random.choice(charset) for _ in range(length))
            if candidate not in generated:
                generated.add(candidate)
                return candidate
        raise RuntimeError("Too many users???? Increase ID length.")
    return generate

letters = generateRandomLetters(10)

@app.route("/")
def home():
    return render_template("app.html")

@app.route("/quizMaker")
def quizMaker():
    return render_template("quizMaker.html")

@app.route("/admin")
def admin():
    return render_template("admin.html")

@socketio.on("pause")
def pause(json):
    global acceptingResponses
    if json.get("admin_code") == adminCode:
        socketio.emit("quiz pause")
        acceptingResponses = False

@socketio.on("resume")
def resume(json):
    global acceptingResponses
    if json.get("admin_code") == adminCode:
        data = bowlJson[currentQuestionIndex]
        question = {
            "id": data.get("id"),
            "text": data.get("question"),
            "choices": [data.get("a"), data.get("b"), data.get("c"), data.get("d"), data.get("e")],
            "selected_answer": None
        }
        socketio.emit("quiz", question)
        acceptingResponses = True

@socketio.on("increment")
def increment(json):
    global acceptingResponses
    global currentQuestionIndex
    if json.get("admin_code") == adminCode:
        if json.get("forward"):
            if currentQuestionIndex + 1 <= bowlJson.__len__() - 1:
                currentQuestionIndex = currentQuestionIndex + 1
        else:
            if currentQuestionIndex - 1 >= 0:
                currentQuestionIndex = currentQuestionIndex - 1
        if acceptingResponses:
            resume({"admin_code": adminCode})
                
@socketio.on("question data")
def questionData():
    global acceptingResponses
    data = bowlJson[currentQuestionIndex]
    if acceptingResponses:
        question = {
            "id": data.get("id"),
            "text": data.get("question"),
            "choices": [data.get("a"), data.get("b"), data.get("c"), data.get("d"), data.get("e")],
            "selected_answer": None
        }
        return question
    else:
        return {}

@socketio.on("submit answer")
def submit(json):
    global acceptingResponses
    
    try:
        #print("Answer submission:", json)
        
        # Validate required fields
        question_id = json.get("question_id")
        team_id = json.get("team_id")
        new_answer = json.get("answer")
        
        if not all([question_id, team_id, new_answer]):
            #print("Missing required fields")
            return {"status":False}
        
        current_question = bowlJson[currentQuestionIndex]
        correct_answer = current_question.get("correct")
        
        # CASE-INSENSITIVE comparison function
        def answers_match(submitted, correct):
            if submitted is None or correct is None:
                return {"status":False}
            return str(submitted).strip().upper() == str(correct).strip().upper()
        
        # Validate question ID and accepting responses
        if question_id != current_question.get("id") or not acceptingResponses:
            #print(f"Invalid submission - ID mismatch or not accepting responses")
            return {"status":False}
        
        # Get team
        team = users.get(team_id)
        if not team:
            #print(f"Team {team_id} not found")
            return {"status":"Team Not Found"}
        
        # Ensure questions structure exists
        if "questions" not in team or not isinstance(team["questions"], dict):
            team["questions"] = {}
        
        # Get previous answer
        previous_answer = None
        if currentQuestionIndex in team["questions"]:
            prev_data = team["questions"][currentQuestionIndex]
            if isinstance(prev_data, dict):
                previous_answer = prev_data.get("answer")  # or prev_data.get("selected_choice")
            else:
                previous_answer = prev_data
        
        # Check if answer is correct
        is_correct = answers_match(new_answer, correct_answer)
        #print(f"Answer '{new_answer}' vs '{correct_answer}' → {'CORRECT' if is_correct else 'INCORRECT'}")
        
        # Calculate score changes
        question_points = current_question.get("score", 1)
        
        if previous_answer:
            was_correct = answers_match(previous_answer, correct_answer)
            
            if was_correct and not is_correct:
                team["score"] = max(0, team["score"] - question_points)
                #print(f"Score decreased by {question_points}")
            elif not was_correct and is_correct:
                team["score"] = team["score"] + question_points
                #print(f"Score increased by {question_points}")
        else:
            # First attempt
            if is_correct:
                team["score"] = team["score"] + question_points
                #print(f"Score increased by {question_points} (first correct)")
        
        # *** UPDATED: Store the answer with selected_choice field ***
        team["questions"][currentQuestionIndex] = {
            "answer": new_answer,  # Keep this for backwards compatibility
            "selected_choice": new_answer.strip().upper(),  # Add this for frontend
            "question_id": question_id,
            "is_correct": is_correct,
            "timestamp": time.time(),
            "question_index": currentQuestionIndex
        }
        
        # ============= CALCULATE ACCURACY =============
        questions = team["questions"]
        if questions:
            total_questions = len(questions)
            correct_count = 0
            
            for q_data in questions.values():
                if isinstance(q_data, dict):
                    if q_data.get("is_correct", False):
                        correct_count += 1
                else:
                    # Handle legacy format - assume it's an answer string
                    # You'd need to check against correct answers, but for now skip
                    pass
            
            team["accuracy"] = round((correct_count / total_questions) * 100, 1) if total_questions > 0 else 0
        else:
            team["accuracy"] = 0
        
        # ============= CALCULATE STREAK =============
        def calculate_streak(team):
            questions = team.get("questions", {})
            if not questions:
                return 0, 0  # current_streak, high_streak
            
            # Get questions in chronological order (by question index)
            sorted_questions = []
            for q_index, q_data in questions.items():
                if isinstance(q_data, dict) and "is_correct" in q_data:
                    sorted_questions.append((q_index, q_data))
            
            # Sort by question index (chronological order)
            sorted_questions.sort(key=lambda x: x[0])
            
            # Calculate current streak (from most recent backwards)
            current_streak = 0
            for q_index, q_data in reversed(sorted_questions):
                if q_data.get("is_correct", False):
                    current_streak += 1
                else:
                    break  # Streak broken
            
            # Calculate highest streak ever achieved
            max_streak = 0
            temp_streak = 0
            for q_index, q_data in sorted_questions:
                if q_data.get("is_correct", False):
                    temp_streak += 1
                    max_streak = max(max_streak, temp_streak)
                else:
                    temp_streak = 0
            
            return current_streak, max_streak
        
        # Update streak values
        current_streak, high_streak = calculate_streak(team)
        team["streak"] = current_streak
        team["highstreak"] = max(team.get("highstreak", 0), high_streak)
        
        # ============= LOGGING =============
        #print(f"=== TEAM STATS UPDATE ===")
        #print(f"Score: {team['score']}")
        #print(f"Accuracy: {team['accuracy']}%")
        #print(f"Current Streak: {team['streak']}")
        #print(f"High Streak: {team['highstreak']}")
        #print(f"Total Questions: {len(team['questions'])}")
        #print(f"Selected Choice: {new_answer.strip().upper()}")
        #print("=== END STATS ===")
        
        return {"status":True}
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return {"status":False}

# ============= HELPER FUNCTION FOR GETTING TEAM STATS =============
def get_team_stats(team_id):
    """Get current team statistics"""
    team = users.get(team_id)
    if not team:
        return None
    
    questions = team.get("questions", {})
    total = len(questions)
    correct = sum(1 for q in questions.values() 
                  if isinstance(q, dict) and q.get("is_correct", False))
    
    return {
        "team_id": team_id,
        "team_number": team.get("team_number"),
        "score": team.get("score", 0),
        "accuracy": team.get("accuracy", 0),
        "current_streak": team.get("streak", 0),
        "high_streak": team.get("highstreak", 0),
        "total_questions": total,
        "correct_answers": correct
    }

@socketio.on("admin teams data")
def teamsData(json):
    if json.get("admin_code") == adminCode:
        return users

@socketio.on("admin clear teams")
def clearTeamsData(json):
    global users
    if json.get("admin_code") == adminCode:
        users = {}

@socketio.on("admin question data")
def questionData(json):
    global acceptingResponses
    data = bowlJson[currentQuestionIndex]
    if json.get("admin_code") == adminCode:
        question = {
            "id": data.get("id"),
            "text": data.get("question"),
            "choices": [data.get("a"), data.get("b"), data.get("c"), data.get("d"), data.get("e")],
            "selected_answer": None,
            "correct_answer": data.get("correct"),
            "number_of_flags": 0,
            "count": bowlJson.__len__(),
            "index": currentQuestionIndex,
            "open": acceptingResponses
        }
        return question

@socketio.on("jump to question")
def jump_to_question(json):
    global currentQuestionIndex
    global acceptingResponses
    
    if json.get("admin_code") == adminCode:
        question_index = json.get("question_index")
        
        # Validate question index
        if question_index is not None and 0 <= question_index < len(bowlJson):
            currentQuestionIndex = question_index
            print(f"Jumped to question index: {currentQuestionIndex}")
            
            # If quiz was active, resume with new question
            if acceptingResponses:
                resume({"admin_code": adminCode})
            
            return {"status": True, "current_index": currentQuestionIndex}
        else:
            return {"status": False, "error": "Invalid question index"}
    
    return {"status": False, "error": "Unauthorized"}

@socketio.on("register team")
def register(json):
    global teamIdIncrement
    teamId = str(json.get("team_id"))
    teamNumber = json.get("team_number")
    member1 = json.get("member1")
    member2 = json.get("member2")
    member3 = json.get("member3")
    country = json.get("country")
    if not users.get(teamId):
        teamId = letters()
        teamNumber = teamIdIncrement
        teamIdIncrement + 1
        users[teamId] = {
            "team_number": teamNumber,
            "member1": member1,
            "member2": member2,
            "member3": member3,
            "country": country,
            "questions": {},
            "score": 0,
            "accuracy": 0,
            "streak": 0,
            "highstreak": 0
        }
    return {
        "team_id": teamId,
        "info": users.get(teamId)
    }

@app.route('/upload', methods=['POST'])
def upload():
    global bowlJson
    global currentQuestionIndex
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if data:
            client.collection("data").update(
                "2pj7n2u43881pk7",
                {
                    "field": data,
                }
            )
            currentQuestionIndex = 0
            bowlJson = getBowlJson()
            return "ok"
    return "no"

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=os.environ.get('PORT', 10000), debug=False)


