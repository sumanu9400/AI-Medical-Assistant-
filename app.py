"""
Medical AI Assistant Chatbot - Flask Application with Streaming
Optimized for Vercel Serverless Deployment
"""
from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
from flask_cors import CORS
from langchain_groq import ChatGroq
from langchain.schema import HumanMessage, SystemMessage
from src.helper import create_vector_store, retrieve_relevant_context
from src.prompt import MEDICAL_SYSTEM_PROMPT, MEDICAL_ASSISTANT_PROMPT, DISCLAIMER_TEXT
import os
import json
import secrets
from dotenv import load_dotenv

# Load environment variables (Vercel uses System Env, but load_dotenv doesn't hurt)
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(16))
CORS(app)

# Global lazy-loaded objects
_llm = None
_llm_no_stream = None
_vector_store = None

def get_llm(streaming=True):
    """Lazy initialization of Groq LLM."""
    global _llm, _llm_no_stream
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    
    try:
        if streaming:
            if _llm is None:
                _llm = ChatGroq(
                    model="llama-3.3-70b-versatile",
                    groq_api_key=api_key,
                    temperature=0.3,
                    max_tokens=2048,
                    streaming=True
                )
            return _llm
        else:
            if _llm_no_stream is None:
                _llm_no_stream = ChatGroq(
                    model="llama-3.3-70b-versatile",
                    groq_api_key=api_key,
                    temperature=0.3,
                    max_tokens=2048,
                    streaming=False
                )
            return _llm_no_stream
    except Exception as e:
        print(f"[ERROR] LLM Init Error: {e}")
        return None

def get_vector_store():
    """Lazy initialization of Vector Store."""
    global _vector_store
    if _vector_store is None:
        try:
            _vector_store = create_vector_store()
        except Exception as e:
            print(f"[WARN] Vector store initialization failed: {e}")
            _vector_store = None
    return _vector_store

def build_messages(user_message, context, chat_history):
    """Build the message list for Groq LLM."""
    prompt = MEDICAL_ASSISTANT_PROMPT.format(
        context=context if context else "No specific medical documents available — rely on your training knowledge.",
        chat_history=chat_history if chat_history else "No previous conversation.",
        question=user_message
    )
    return [
        SystemMessage(content=MEDICAL_SYSTEM_PROMPT),
        HumanMessage(content=prompt)
    ]

def get_context_and_history(user_message):
    """Retrieve RAG context and format chat history from session."""
    context = ""
    vs = get_vector_store()
    if vs:
        try:
            context = retrieve_relevant_context(user_message, vs, k=4)
        except Exception:
            context = ""

    chat_history = ""
    for msg in session.get("conversation_history", [])[-6:]:
        chat_history += f"{msg['role']}: {msg['content']}\n"

    return context, chat_history

@app.route('/')
def index():
    """Serve the main chat interface."""
    return render_template('index.html')

@app.route('/api/health')
def health():
    """Health check endpoint."""
    llm_instance = get_llm()
    vs_instance = get_vector_store()
    return jsonify({
        "status": "ok",
        "llm": "ready" if llm_instance else "unavailable (check GROQ_API_KEY)",
        "vector_store": "ready" if vs_instance else "unavailable (check index configuration)"
    })

@app.route('/api/stream', methods=['POST'])
def stream_chat():
    llm = get_llm(streaming=True)
    if llm is None:
        return jsonify({'success': False, 'error': 'AI service is not configured (check Environment Variables)'}), 503

    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()

        if not user_message:
            return jsonify({'success': False, 'error': 'Message cannot be empty'}), 400

        if 'conversation_history' not in session:
            session['conversation_history'] = []

        context, chat_history = get_context_and_history(user_message)
        messages = build_messages(user_message, context, chat_history)

        def generate():
            full_response = ""
            try:
                for chunk in llm.stream(messages):
                    token = chunk.content
                    if token:
                        full_response += token
                        yield f"data: {json.dumps({'token': token})}\n\n"

                needs_disclaimer = any(
                    kw in user_message.lower()
                    for kw in ['diagnose', 'treatment', 'medicine', 'drug', 'symptom',
                               'pain', 'sick', 'disease', 'infection', 'fever', 'cancer']
                )
                if needs_disclaimer:
                    full_response += DISCLAIMER_TEXT
                    yield f"data: {json.dumps({'token': DISCLAIMER_TEXT})}\n\n"

                session['conversation_history'].append({'role': 'User', 'content': user_message})
                session['conversation_history'].append({'role': 'Assistant', 'content': full_response})

                if len(session['conversation_history']) > 20:
                    session['conversation_history'] = session['conversation_history'][-20:]
                session.modified = True
                yield f"data: {json.dumps({'done': True})}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return Response(stream_with_context(generate()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    except Exception as e:
        return jsonify({'success': False, 'error': 'An internal error occurred'}), 500

@app.route('/api/chat', methods=['POST'])
def chat():
    llm_no_stream = get_llm(streaming=False)
    if llm_no_stream is None:
        return jsonify({'success': False, 'error': 'AI service is not configured'}), 503

    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()
        if 'conversation_history' not in session:
            session['conversation_history'] = []

        context, chat_history = get_context_and_history(user_message)
        messages = build_messages(user_message, context, chat_history)
        response = llm_no_stream.invoke(messages)
        ai_response = response.content

        needs_disclaimer = any(kw in user_message.lower() for kw in ['diagnose', 'treatment', 'medicine'])
        if needs_disclaimer:
            ai_response += DISCLAIMER_TEXT

        session['conversation_history'].append({'role': 'User', 'content': user_message})
        session['conversation_history'].append({'role': 'Assistant', 'content': ai_response})
        session.modified = True
        return jsonify({'success': True, 'response': ai_response})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/clear', methods=['POST'])
def clear_conversation():
    session['conversation_history'] = []
    session.modified = True
    return jsonify({'success': True, 'message': 'Conversation cleared'})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)