"""
Medical AI Assistant Chatbot - Flask Application with Streaming
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

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
CORS(app)

# Initialize Groq LLM — use a large, accurate model
try:
    llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        groq_api_key=os.getenv("GROQ_API_KEY"),
        temperature=0.3,
        max_tokens=2048,
        streaming=True
    )
    llm_no_stream = ChatGroq(
        model="llama-3.3-70b-versatile",
        groq_api_key=os.getenv("GROQ_API_KEY"),
        temperature=0.3,
        max_tokens=2048,
        streaming=False
    )
    print("[OK] Groq LLM (llama-3.3-70b-versatile) initialized successfully")
except Exception as e:
    print(f"[ERROR] Error initializing Groq LLM: {e}")
    llm = None
    llm_no_stream = None

# Initialize vector store (optional - for RAG)
try:
    vector_store = create_vector_store()
    print("[OK] Vector store initialized successfully")
except Exception as e:
    print(f"[WARN] Vector store not available: {e}")
    vector_store = None


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
    # RAG context
    context = ""
    if vector_store:
        try:
            context = retrieve_relevant_context(user_message, vector_store, k=4)
        except Exception:
            context = ""

    # Chat history (last 6 messages = 3 exchanges)
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
    return jsonify({
        "status": "ok",
        "llm": "ready" if llm else "unavailable",
        "vector_store": "ready" if vector_store else "unavailable"
    })


@app.route('/api/stream', methods=['POST'])
def stream_chat():
    """
    Streaming chat endpoint using Server-Sent Events (SSE).
    Returns tokens one-by-one for real-time display.
    """
    if llm is None:
        return jsonify({'success': False, 'error': 'AI service is not available'}), 503

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
                        # SSE format: data: <json>\n\n
                        yield f"data: {json.dumps({'token': token})}\n\n"

                # Add disclaimer for medical topics
                needs_disclaimer = any(
                    kw in user_message.lower()
                    for kw in ['diagnose', 'treatment', 'medicine', 'drug', 'symptom',
                               'pain', 'sick', 'disease', 'infection', 'fever', 'cancer']
                )
                if needs_disclaimer:
                    full_response += DISCLAIMER_TEXT
                    yield f"data: {json.dumps({'token': DISCLAIMER_TEXT})}\n\n"

                # Save to session history
                session['conversation_history'].append({'role': 'User', 'content': user_message})
                session['conversation_history'].append({'role': 'Assistant', 'content': full_response})

                # Keep last 20 messages
                if len(session['conversation_history']) > 20:
                    session['conversation_history'] = session['conversation_history'][-20:]
                session.modified = True

                # Signal end of stream
                yield f"data: {json.dumps({'done': True})}\n\n"

            except Exception as e:
                print(f"[ERROR] Streaming error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive'
            }
        )

    except Exception as e:
        print(f"[ERROR] Stream endpoint: {e}")
        return jsonify({'success': False, 'error': 'An error occurred'}), 500


@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Non-streaming fallback chat endpoint.
    """
    if llm_no_stream is None:
        return jsonify({'success': False, 'error': 'AI service is not available'}), 503

    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()

        if not user_message:
            return jsonify({'success': False, 'error': 'Message cannot be empty'}), 400

        if 'conversation_history' not in session:
            session['conversation_history'] = []

        context, chat_history = get_context_and_history(user_message)
        messages = build_messages(user_message, context, chat_history)

        response = llm_no_stream.invoke(messages)
        ai_response = response.content

        # Add disclaimer
        needs_disclaimer = any(
            kw in user_message.lower()
            for kw in ['diagnose', 'treatment', 'medicine', 'drug', 'symptom',
                       'pain', 'sick', 'disease', 'infection', 'fever', 'cancer']
        )
        if needs_disclaimer:
            ai_response += DISCLAIMER_TEXT

        session['conversation_history'].append({'role': 'User', 'content': user_message})
        session['conversation_history'].append({'role': 'Assistant', 'content': ai_response})

        if len(session['conversation_history']) > 20:
            session['conversation_history'] = session['conversation_history'][-20:]
        session.modified = True

        return jsonify({'success': True, 'response': ai_response})

    except Exception as e:
        print(f"[ERROR] Chat endpoint: {e}")
        return jsonify({'success': False, 'error': f'Error: {str(e)}'}), 500






@app.route('/api/clear', methods=['POST'])
def clear_conversation():
    """Clear conversation history."""
    try:
        session['conversation_history'] = []
        session.modified = True
        return jsonify({'success': True, 'message': 'Conversation cleared'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("Medical AI Assistant Chatbot")
    print("=" * 60)
    print("Server starting on http://localhost:5000")
    print("Press Ctrl+C to stop")
    print("=" * 60 + "\n")

    app.run(debug=True, host='0.0.0.0', port=5000)
