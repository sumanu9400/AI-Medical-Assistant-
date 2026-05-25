"""
Helper functions for medical chatbot
"""
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, DirectoryLoader
from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore
from pinecone import Pinecone, ServerlessSpec
import os
from dotenv import load_dotenv

load_dotenv()

def load_pdf_documents(data_path):
    """
    Load PDF documents from a directory
    
    Args:
        data_path: Path to directory containing PDF files
        
    Returns:
        List of loaded documents
    """
    try:
        loader = DirectoryLoader(
            data_path,
            glob="*.pdf",
            loader_cls=PyPDFLoader
        )
        documents = loader.load()
        return documents
    except Exception as e:
        print(f"Error loading PDFs: {e}")
        return []

def create_text_chunks(documents, chunk_size=500, chunk_overlap=50):
    """
    Split documents into smaller chunks for processing
    
    Args:
        documents: List of documents to split
        chunk_size: Size of each chunk
        chunk_overlap: Overlap between chunks
        
    Returns:
        List of text chunks
    """
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap
    )
    chunks = text_splitter.split_documents(documents)
    return chunks

def initialize_pinecone():
    """
    Initialize Pinecone client and create index if needed
    
    Returns:
        Pinecone client instance
    """
    try:
        api_key = os.getenv("PINECONE_API_KEY")
        pc = Pinecone(api_key=api_key)
        return pc
    except Exception as e:
        print(f"Error initializing Pinecone: {e}")
        return None

def create_vector_store(index_name="medical-chatbot"):
    """
    Create or connect to Pinecone vector store
    
    Args:
        index_name: Name of the Pinecone index
        
    Returns:
        PineconeVectorStore instance
    """
    try:
        embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small",
            openai_api_key=os.getenv("OPENAI_API_KEY")
        )
        
        # Connect to existing index or create new one
        vector_store = PineconeVectorStore(
            index_name=index_name,
            embedding=embeddings
        )
        
        return vector_store
    except Exception as e:
        print(f"Error creating vector store: {e}")
        return None

def add_documents_to_vectorstore(documents, index_name="medical-chatbot"):
    """
    Add documents to Pinecone vector store
    
    Args:
        documents: List of documents to add
        index_name: Name of the Pinecone index
    """
    try:
        embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small",
            openai_api_key=os.getenv("OPENAI_API_KEY")
        )
        
        # Create chunks
        chunks = create_text_chunks(documents)
        
        # Add to vector store
        PineconeVectorStore.from_documents(
            documents=chunks,
            embedding=embeddings,
            index_name=index_name
        )
        
        print(f"Successfully added {len(chunks)} chunks to vector store")
    except Exception as e:
        print(f"Error adding documents to vector store: {e}")

def retrieve_relevant_context(query, vector_store, k=3):
    """
    Retrieve relevant context from vector store
    
    Args:
        query: User query
        vector_store: PineconeVectorStore instance
        k: Number of documents to retrieve
        
    Returns:
        String of relevant context
    """
    try:
        if vector_store is None:
            return ""
        
        # Perform similarity search
        docs = vector_store.similarity_search(query, k=k)
        
        # Combine retrieved documents
        context = "\n\n".join([doc.page_content for doc in docs])
        return context
    except Exception as e:
        print(f"Error retrieving context: {e}")
        return ""
