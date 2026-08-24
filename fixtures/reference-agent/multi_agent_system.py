"""
Multi-Agent System with Manager-Sub Agent Architecture using CrewAI
================================================================

This system implements a Manager Agent that coordinates with multiple Sub-Agents
for parallel task execution across different data sources using Gemini API.
All tools return comprehensive mock responses for demonstration.
"""

import asyncio
import json
import time
import random
from typing import Dict, List, Any, Optional, Union
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from datetime import datetime, timedelta

# CrewAI imports
from crewai import Agent, Task, Crew, Process
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# Langchain imports for Gemini
from langchain_google_genai import ChatGoogleGenerativeAI

import sqlite3
import numpy as np

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class AgentResponse:
    """Standardized response format for all agents"""
    agent_id: str
    task_id: str
    data: Any
    confidence_score: float
    metadata: Dict[str, Any]
    execution_time: float
    success: bool
    error_message: Optional[str] = None
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

class QueryInput(BaseModel):
    """Input model for user queries"""
    query: str = Field(description="The user's query")
    filters: Optional[Dict[str, Any]] = Field(default=None, description="Optional filters")
    priority: str = Field(default="medium", description="Query priority: low, medium, high")
    max_parallel_tasks: int = Field(default=3, description="Maximum parallel tasks")

# =============================================================================
# MOCK DATA GENERATORS
# =============================================================================

class MockDataGenerator:
    """Generates realistic mock data for all tools"""
    
    @staticmethod
    def generate_web_search_results(query: str, num_results: int = 5) -> List[Dict[str, Any]]:
        """Generate mock web search results"""
        
        # Topic-specific mock results
        mock_results = {
            "artificial intelligence": [
                {
                    "title": "The Future of Artificial Intelligence in 2024",
                    "url": "https://techcrunch.com/ai-future-2024",
                    "snippet": "AI continues to revolutionize industries with breakthrough developments in machine learning, natural language processing, and computer vision.",
                    "source": "TechCrunch",
                    "date": "2024-12-15"
                },
                {
                    "title": "AI Ethics and ResponsibleDevelopment",
                    "url": "https://mit.edu/ai-ethics-2024",
                    "snippet": "Research institutions emphasize the importance of ethical AI development and addressing bias in machine learning models.",
                    "source": "MIT Technology Review",
                    "date": "2024-12-10"
                }
            ],
            "machine learning": [
                {
                    "title": "Top Machine Learning Frameworks in 2024",
                    "url": "https://medium.com/ml-frameworks-2024",
                    "snippet": "TensorFlow, PyTorch, and Scikit-learn remain the most popular frameworks for machine learning development.",
                    "source": "Medium",
                    "date": "2024-12-08"
                },
                {
                    "title": "AutoML: Democratizing Machine Learning",
                    "url": "https://google.ai/automl-advances",
                    "snippet": "Automated machine learning tools are making ML accessible to non-experts across various industries.",
                    "source": "Google AI Blog",
                    "date": "2024-12-05"
                }
            ],
            "cloud computing": [
                {
                    "title": "Cloud Computing Trends and Predictions for 2025",
                    "url": "https://aws.amazon.com/cloud-trends-2025",
                    "snippet": "Multi-cloud strategies, serverless computing, and edge computing are driving the next wave of cloud adoption.",
                    "source": "AWS Blog",
                    "date": "2024-12-12"
                },
                {
                    "title": "Comparing Major Cloud Providers: AWS vs Azure vs GCP",
                    "url": "https://cloudcompare.net/providers-2024",
                    "snippet": "Comprehensive comparison of features, pricing, and performance across major cloud platforms.",
                    "source": "Cloud Compare",
                    "date": "2024-12-07"
                }
            ]
        }
        
        # Find the best matching topic
        query_lower = query.lower()
        best_match = None
        for topic in mock_results.keys():
            if topic in query_lower:
                best_match = topic
                break
        
        if best_match:
            base_results = mock_results[best_match]
        else:
            # Generic results for unmatched queries
            base_results = [
                {
                    "title": f"Information about {query.title()}",
                    "url": f"https://example.com/{query.replace(' ', '-').lower()}",
                    "snippet": f"Comprehensive information and latest updates about {query.lower()}.",
                    "source": "Example Source",
                    "date": datetime.now().strftime("%Y-%m-%d")
                }
            ]
        
        # Extend results if needed
        results = base_results.copy()
        while len(results) < num_results:
            result = results[len(results) % len(base_results)].copy()
            result["title"] += f" - Part {len(results) + 1}"
            result["url"] += f"-part-{len(results) + 1}"
            results.append(result)
        
        return results[:num_results]
    
    @staticmethod
    def generate_sql_results(query: str) -> List[Dict[str, Any]]:
        """Generate mock SQL query results"""
        
        query_lower = query.lower()
        
        if "products" in query_lower:
            if "electronics" in query_lower:
                return [
                    {"id": 1, "name": "Laptop Pro", "category": "Electronics", "price": 1299.99, "description": "High-performance laptop"},
                    {"id": 2, "name": "Wireless Mouse", "category": "Electronics", "price": 49.99, "description": "Ergonomic wireless mouse"},
                    {"id": 5, "name": "Smartphone X", "category": "Electronics", "price": 899.99, "description": "Latest smartphone model"},
                    {"id": 8, "name": "Tablet Pro", "category": "Electronics", "price": 599.99, "description": "Professional tablet device"}
                ]
            elif "price" in query_lower and ("less" in query_lower or "<" in query_lower):
                return [
                    {"id": 2, "name": "Wireless Mouse", "category": "Electronics", "price": 49.99, "description": "Ergonomic wireless mouse"},
                    {"id": 4, "name": "USB Cable", "category": "Electronics", "price": 12.99, "description": "High-speed USB cable"},
                    {"id": 6, "name": "Phone Case", "category": "Accessories", "price": 24.99, "description": "Protective phone case"},
                    {"id": 7, "name": "Screen Cleaner", "category": "Accessories", "price": 8.99, "description": "Monitor screen cleaner"}
                ]
            else:
                return [
                    {"id": 1, "name": "Laptop Pro", "category": "Electronics", "price": 1299.99, "description": "High-performance laptop"},
                    {"id": 2, "name": "Wireless Mouse", "category": "Electronics", "price": 49.99, "description": "Ergonomic wireless mouse"},
                    {"id": 3, "name": "Office Chair", "category": "Furniture", "price": 299.99, "description": "Comfortable ergonomic chair"},
                    {"id": 4, "name": "USB Cable", "category": "Electronics", "price": 12.99, "description": "High-speed USB cable"},
                    {"id": 5, "name": "Smartphone X", "category": "Electronics", "price": 899.99, "description": "Latest smartphone model"}
                ]
        
        elif "users" in query_lower:
            if "2024" in query_lower:
                return [
                    {"id": 1, "name": "John Doe", "email": "john@example.com", "registration_date": "2024-01-15"},
                    {"id": 2, "name": "Jane Smith", "email": "jane@example.com", "registration_date": "2024-02-20"},
                    {"id": 3, "name": "Bob Johnson", "email": "bob@example.com", "registration_date": "2024-03-10"},
                    {"id": 4, "name": "Alice Brown", "email": "alice@example.com", "registration_date": "2024-04-05"},
                    {"id": 5, "name": "Charlie Wilson", "email": "charlie@example.com", "registration_date": "2024-05-18"}
                ]
            else:
                return [
                    {"id": 1, "name": "John Doe", "email": "john@example.com", "registration_date": "2024-01-15"},
                    {"id": 2, "name": "Jane Smith", "email": "jane@example.com", "registration_date": "2024-02-20"},
                    {"id": 6, "name": "David Lee", "email": "david@example.com", "registration_date": "2023-11-12"},
                    {"id": 7, "name": "Sarah Miller", "email": "sarah@example.com", "registration_date": "2023-09-28"}
                ]
        
        elif "count" in query_lower or "total" in query_lower:
            if "products" in query_lower:
                return [{"count": 15, "table": "products"}]
            elif "users" in query_lower:
                return [{"count": 127, "table": "users"}]
        
        elif "avg" in query_lower or "average" in query_lower:
            if "price" in query_lower:
                return [{"average_price": 284.67, "category": "all_products"}]
        
        else:
            # Generic response for unrecognized queries
            return [{"message": "Query executed successfully", "affected_rows": 0}]
    
    @staticmethod
    def generate_vector_search_results(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Generate mock vector search results"""
        
        # Comprehensive knowledge base for semantic matching
        knowledge_base = {
            "machine learning": [
                {"document": "Machine learning algorithms enable computers to learn patterns from data without explicit programming", "similarity": 0.95},
                {"document": "Supervised learning uses labeled data to train models for prediction and classification tasks", "similarity": 0.89},
                {"document": "Unsupervised learning discovers hidden patterns in data without labeled examples", "similarity": 0.85},
                {"document": "Deep learning uses neural networks with multiple layers to model complex patterns", "similarity": 0.92},
                {"document": "Feature engineering is crucial for improving machine learning model performance", "similarity": 0.81}
            ],
            "artificial intelligence": [
                {"document": "Artificial intelligence aims to create systems that can perform tasks requiring human intelligence", "similarity": 0.94},
                {"document": "AI applications include natural language processing, computer vision, and robotics", "similarity": 0.88},
                {"document": "Machine learning is a subset of artificial intelligence focused on learning from data", "similarity": 0.91},
                {"document": "Expert systems use knowledge bases and inference engines to solve complex problems", "similarity": 0.79},
                {"document": "AI ethics addresses fairness, transparency, and accountability in intelligent systems", "similarity": 0.83}
            ],
            "natural language processing": [
                {"document": "Natural language processing enables computers to understand and generate human language", "similarity": 0.96},
                {"document": "Text preprocessing includes tokenization, stemming, and removing stop words", "similarity": 0.82},
                {"document": "Named entity recognition identifies people, places, and organizations in text", "similarity": 0.87},
                {"document": "Sentiment analysis determines emotional tone and opinions in textual data", "similarity": 0.84},
                {"document": "Large language models like GPT have revolutionized NLP applications", "similarity": 0.90}
            ],
            "deep learning": [
                {"document": "Deep learning uses artificial neural networks with multiple hidden layers", "similarity": 0.93},
                {"document": "Convolutional neural networks excel at image recognition and computer vision tasks", "similarity": 0.88},
                {"document": "Recurrent neural networks are designed for sequential data and time series", "similarity": 0.85},
                {"document": "Backpropagation algorithm trains neural networks by adjusting weights and biases", "similarity": 0.80},
                {"document": "Transfer learning leverages pre-trained models for new tasks with limited data", "similarity": 0.86}
            ],
            "cloud computing": [
                {"document": "Cloud computing provides on-demand access to computing resources over the internet", "similarity": 0.94},
                {"document": "Infrastructure as a Service (IaaS) offers virtualized computing infrastructure", "similarity": 0.87},
                {"document": "Platform as a Service (PaaS) provides development platforms and tools", "similarity": 0.85},
                {"document": "Software as a Service (SaaS) delivers applications over the internet", "similarity": 0.83},
                {"document": "Multi-cloud strategies use multiple cloud providers for redundancy and optimization", "similarity": 0.81}
            ],
            "data science": [
                {"document": "Data science combines statistics, programming, and domain expertise to extract insights", "similarity": 0.92},
                {"document": "Data visualization helps communicate complex patterns and findings effectively", "similarity": 0.86},
                {"document": "Statistical analysis provides methods for understanding data distributions and relationships", "similarity": 0.84},
                {"document": "Data cleaning and preprocessing are essential steps in any data science project", "similarity": 0.88},
                {"document": "Predictive analytics uses historical data to forecast future trends and outcomes", "similarity": 0.90}
            ]
        }
        
        # Find best matching topic
        query_lower = query.lower()
        best_match = None
        max_relevance = 0
        
        for topic in knowledge_base.keys():
            if topic in query_lower:
                relevance = len(topic) / len(query_lower)  # Simple relevance scoring
                if relevance > max_relevance:
                    max_relevance = relevance
                    best_match = topic
        
        if best_match:
            base_results = knowledge_base[best_match]
        else:
            # Generic semantic matches for unrecognized queries
            base_results = [
                {"document": f"Information about {query} and related concepts in the field", "similarity": 0.75},
                {"document": f"Technical overview of {query} with practical applications", "similarity": 0.70},
                {"document": f"Latest developments and trends in {query} research", "similarity": 0.68},
                {"document": f"Best practices and methodologies for {query} implementation", "similarity": 0.72},
                {"document": f"Comparative analysis of different approaches to {query}", "similarity": 0.69}
            ]
        
        # Sort by similarity and return top-k
        sorted_results = sorted(base_results, key=lambda x: x["similarity"], reverse=True)
        results = []
        
        for i, item in enumerate(sorted_results[:top_k]):
            results.append({
                "document": item["document"],
                "similarity_score": item["similarity"],
                "rank": i + 1,
                "metadata": {
                    "source": "knowledge_base",
                    "topic": best_match or "general",
                    "indexed_date": (datetime.now() - timedelta(days=random.randint(1, 365))).strftime("%Y-%m-%d")
                }
            })
        
        return results

# =============================================================================
# ENHANCED MOCK TOOLS
# =============================================================================

class MockWebSearchTool(BaseTool):
    """Enhanced mock web search tool with realistic responses"""
    name: str = "web_search"
    description: str = "Search the web for information using mock data"
    
    def _run(self, query: str) -> Dict[str, Any]:
        # Simulate network delay
        time.sleep(random.uniform(0.5, 1.5))
        
        try:
            start_time = time.time()
            
            # Generate mock results
            results = MockDataGenerator.generate_web_search_results(query)
            
            execution_time = time.time() - start_time
            
            # Calculate confidence based on query specificity
            confidence = min(0.95, 0.6 + (len(query.split()) * 0.05))
            
            return {
                "results": results,
                "source": "web_search",
                "confidence": confidence,
                "execution_time": execution_time,
                "success": True,
                "total_results": len(results),
                "metadata": {
                    "query": query,
                    "search_engine": "MockSearchEngine",
                    "timestamp": datetime.now().isoformat(),
                    "result_types": ["news", "articles", "blogs"],
                    "language": "en"
                }
            }
            
        except Exception as e:
            logger.error(f"Mock web search failed: {str(e)}")
            return {
                "results": None,
                "source": "web_search",
                "confidence": 0.0,
                "execution_time": 0,
                "success": False,
                "error": str(e),
                "metadata": {"query": query}
            }

class MockSQLTool(BaseTool):
    """Enhanced mock SQL tool with realistic database responses"""
    name: str = "sql_query"
    description: str = "Execute SQL queries against mock database"
    
    def _run(self, query: str) -> Dict[str, Any]:
        # Simulate database query time
        time.sleep(random.uniform(0.2, 0.8))
        
        try:
            start_time = time.time()
            
            # Generate mock results based on query
            results = MockDataGenerator.generate_sql_results(query)
            
            execution_time = time.time() - start_time
            
            # High confidence for structured data
            confidence = 0.95 if results else 0.0
            
            return {
                "results": results,
                "source": "sql_database",
                "confidence": confidence,
                "execution_time": execution_time,
                "success": True,
                "row_count": len(results),
                "metadata": {
                    "query": query,
                    "database": "MockDB",
                    "timestamp": datetime.now().isoformat(),
                    "tables_accessed": self._extract_tables_from_query(query),
                    "query_type": self._determine_query_type(query)
                }
            }
            
        except Exception as e:
            logger.error(f"Mock SQL query failed: {str(e)}")
            return {
                "results": None,
                "source": "sql_database",
                "confidence": 0.0,
                "execution_time": 0,
                "success": False,
                "error": str(e),
                "metadata": {"query": query}
            }
    
    def _extract_tables_from_query(self, query: str) -> List[str]:
        """Extract table names from SQL query"""
        tables = []
        query_lower = query.lower()
        if "products" in query_lower:
            tables.append("products")
        if "users" in query_lower:
            tables.append("users")
        return tables or ["unknown"]
    
    def _determine_query_type(self, query: str) -> str:
        """Determine the type of SQL query"""
        query_lower = query.lower().strip()
        if query_lower.startswith("select"):
            return "SELECT"
        elif query_lower.startswith("insert"):
            return "INSERT"
        elif query_lower.startswith("update"):
            return "UPDATE"
        elif query_lower.startswith("delete"):
            return "DELETE"
        else:
            return "OTHER"

class MockVectorSearchTool(BaseTool):
    """Enhanced mock vector search tool with semantic similarity"""
    name: str = "vector_search"
    description: str = "Perform semantic search using mock vector database"
    
    def _run(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        # Simulate vector computation time
        time.sleep(random.uniform(0.3, 1.0))
        
        try:
            start_time = time.time()
            
            # Generate mock semantic search results
            results = MockDataGenerator.generate_vector_search_results(query, top_k)
            
            execution_time = time.time() - start_time
            
            # Confidence based on top similarity score
            confidence = results[0]["similarity_score"] if results else 0.0
            
            return {
                "results": results,
                "source": "vector_database",
                "confidence": confidence,
                "execution_time": execution_time,
                "success": True,
                "total_documents": 1000,  # Mock total document count
                "metadata": {
                    "query": query,
                    "vector_db": "MockVectorDB",
                    "timestamp": datetime.now().isoformat(),
                    "embedding_model": "mock-embeddings-v1",
                    "similarity_metric": "cosine",
                    "top_k": top_k,
                    "query_vector_dim": 384
                }
            }
            
        except Exception as e:
            logger.error(f"Mock vector search failed: {str(e)}")
            return {
                "results": None,
                "source": "vector_database",
                "confidence": 0.0,
                "execution_time": 0,
                "success": False,
                "error": str(e),
                "metadata": {"query": query}
            }

# =============================================================================
# MULTI-AGENT SYSTEM WITH GEMINI
# =============================================================================

class MultiAgentSystem:
    """Main system orchestrating the multi-agent workflow using Gemini"""
    
    def __init__(self, gemini_api_key: str = None):
        # Initialize Gemini LLM
        if gemini_api_key:
            self.llm = ChatGoogleGenerativeAI(
                model="gemini/gemini-2.0-flash",
                google_api_key=gemini_api_key,
                temperature=0.1,
                convert_system_message_to_human=True
            )
        else:
            # Use environment variable
            self.llm = ChatGoogleGenerativeAI(
                model="gemini/gemini-2.0-flash",
                temperature=0.1,
                convert_system_message_to_human=True
            )
            
        self.tools = self._initialize_tools()
        self.agents = self._create_agents()
        self.execution_history = []
        
    def _initialize_tools(self) -> Dict[str, BaseTool]:
        """Initialize all mock tools"""
        return {
            "web_search": MockWebSearchTool(),
            "sql_query": MockSQLTool(),
            "vector_search": MockVectorSearchTool()
        }
    
    def _create_agents(self) -> Dict[str, Agent]:
        """Create all specialized agents with enhanced prompts"""
        
        # Manager Agent
        manager_agent = Agent(
            role="Query Manager and Coordinator",
            goal="Analyze user queries, delegate tasks to appropriate sub-agents, and synthesize comprehensive responses",
            backstory="""You are an intelligent multi-agent system coordinator with expertise in 
            breaking down complex queries into parallelizable sub-tasks. You excel at:
            
            1. Understanding query intent and complexity
            2. Identifying which data sources are most relevant
            3. Delegating tasks to specialized agents efficiently
            4. Synthesizing responses from multiple sources
            5. Resolving conflicts and inconsistencies in data
            6. Providing confidence assessments for final answers
            
            You work with three specialized agents:
            - WebSearch Agent: For current information, news, trends, and general knowledge
            - SQL Agent: For structured data queries, statistics, and database operations
            - Vector Agent: For semantic search, conceptual matching, and similarity queries
            
            Always provide structured, comprehensive responses that cite sources and include confidence levels.""",
            llm=self.llm,
            verbose=True,
            allow_delegation=True,
            max_retry_limit=2
        )
        
        # Web Search Agent
        web_agent = Agent(
            role="Web Information Research Specialist",
            goal="Find current, relevant, and credible information from web sources",
            backstory="""You are a specialized web research expert who excels at finding 
            up-to-date information from various web sources. Your expertise includes:
            
            1. Crafting effective search queries for maximum relevance
            2. Evaluating source credibility and information quality
            3. Identifying current trends, news, and developments
            4. Finding diverse perspectives on complex topics
            5. Extracting key insights from web content
            
            You always provide detailed source information, publication dates, and 
            assess the reliability of the information you find. Your responses include
            confidence scores based on source quality and information recency.""",
            tools=[self.tools["web_search"]],
            llm=self.llm,
            verbose=True
        )
        
        # SQL Database Agent
        sql_agent = Agent(
            role="Database Query and Analytics Specialist",
            goal="Extract, analyze, and summarize structured data from relational databases",
            backstory="""You are a database expert with deep knowledge of SQL and data analysis. 
            Your specialties include:
            
            1. Writing efficient and optimized SQL queries
            2. Understanding database schemas and relationships
            3. Performing statistical analysis on structured data
            4. Generating insights from numerical and categorical data
            5. Handling data quality issues and missing values
            
            Available database schema:
            - products: id, name, category, price, description
            - users: id, name, email, registration_date
            
            You provide precise data analysis with statistical summaries and always 
            validate query results for accuracy and completeness.""",
            tools=[self.tools["sql_query"]],
            llm=self.llm,
            verbose=True
        )
        
        # Vector Database Agent
        vector_agent = Agent(
            role="Semantic Search and Similarity Specialist",
            goal="Perform semantic search and find conceptually similar content using vector embeddings",
            backstory="""You are a semantic search expert specializing in understanding 
            the deeper meaning and context of queries. Your capabilities include:
            
            1. Understanding semantic similarity beyond keyword matching
            2. Finding conceptually related content and ideas
            3. Identifying patterns and themes in unstructured data
            4. Performing similarity-based recommendations
            5. Analyzing conceptual relationships between topics
            
            You work with a comprehensive knowledge base covering technology, science, 
            business, and general knowledge. Your responses include similarity scores 
            and explain the semantic relationships you discover.""",
            tools=[self.tools["vector_search"]],
            llm=self.llm,
            verbose=True
        )
        
        return {
            "manager": manager_agent,
            "web_search": web_agent,
            "sql_database": sql_agent,
            "vector_search": vector_agent
        }
    
    def _create_tasks(self, query: QueryInput) -> List[Task]:
        """Create comprehensive tasks based on the input query"""
        tasks = []
        
        # Manager coordination task
        manager_task = Task(
            description=f"""
            COORDINATION TASK: Analyze and coordinate response to user query: "{query.query}"
            
            Your comprehensive responsibilities:
            
            1. QUERY ANALYSIS:
               - Understand the user's intent and information needs
               - Identify the complexity level and scope of the query
               - Determine what types of information would best answer the query
            
            2. AGENT COORDINATION:
               - Decide which sub-agents should be involved based on:
                 * WebSearch Agent: For current events, trends, news, general information
                 * SQL Agent: For structured data, statistics, product/user information
                 * Vector Agent: For semantic concepts, related topics, similarity searches
               - Coordinate parallel execution of sub-tasks
            
            3. RESPONSE SYNTHESIS:
               - Collect and analyze responses from all relevant agents
               - Identify complementary information and resolve any conflicts
               - Create a comprehensive, well-structured final response
               - Include confidence assessment and source attribution
            
            4. QUALITY ASSURANCE:
               - Ensure response completeness and accuracy
               - Highlight any limitations or areas of uncertainty
               - Provide clear source citations and confidence levels
            
            EXPECTED OUTPUT: A comprehensive, well-structured response that directly addresses 
            the user's query with synthesized information from relevant sources, including 
            confidence scores and source attribution.
            """,
            agent=self.agents["manager"],
            expected_output="Comprehensive response with synthesized information, source citations, and confidence assessment"
        )
        
        # Web search task
        web_task = Task(
            description=f"""
            WEB RESEARCH TASK: Search for current and relevant web information about: "{query.query}"
            
            Your specific objectives:
            1. Find current, up-to-date information related to the query
            2. Identify multiple credible sources and perspectives
            3. Extract key facts, trends, and insights
            4. Assess information quality and source reliability
            5. Focus on recent developments and current context
            
            Search Strategy:
            - Use effective keywords and search terms
            - Look for authoritative sources (news sites, academic institutions, industry leaders)
            - Consider multiple viewpoints and recent updates
            - Evaluate source credibility and publication dates
            
            EXPECTED OUTPUT: Structured web search results with source information, 
            publication dates, key insights, and reliability assessment.
            """,
            agent=self.agents["web_search"],
            expected_output="Web search results with credible sources, key insights, and reliability scores"
        )
        
        # SQL database task
        sql_task = Task(
            description=f"""
            DATABASE ANALYSIS TASK: Analyze if query "{query.query}" requires database information and provide relevant data.
            
            Your analysis should cover:
            1. Determine if the query relates to available database entities:
               - Products (electronics, furniture, etc.)
               - Users and registration information
               - Pricing and category data
               - Statistical summaries
            
            2. If relevant, provide appropriate data:
               - Execute relevant queries to fetch requested information
               - Calculate statistics, averages, counts as needed
               - Filter and sort data according to query requirements
               - Provide data quality assessment
            
            3. If not relevant, explain why database information doesn't apply
            
            Available Data:
            - Products: Various categories with pricing and descriptions
            - Users: Registration information and user details
            - Support for filtering, aggregation, and statistical analysis
            
            EXPECTED OUTPUT: Either relevant database results with analysis and insights,
            or clear explanation of why database information is not applicable to this query.
            """,
            agent=self.agents["sql_database"],
            expected_output="Database analysis results or explanation of non-applicability with reasoning"
        )
        
        # Vector search task
        vector_task = Task(
            description=f"""
            SEMANTIC SEARCH TASK: Find semantically related content and concepts for: "{query.query}"
            
            Your semantic analysis should include:
            1. Understanding the conceptual meaning beyond literal keywords
            2. Finding related topics, concepts, and ideas
            3. Identifying semantic patterns and relationships
            4. Providing similarity-based insights and connections
            5. Explaining the conceptual relevance of found content
            
            Semantic Focus Areas:
            - Core