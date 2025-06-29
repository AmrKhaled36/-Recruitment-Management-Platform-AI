import pdfplumber
import re
import json
from groq import Groq
from decouple import config
import io
from minio import Minio
import asyncpg
from groq import AsyncGroq
from aiobotocore.session import get_session
from aiokafka import AIOKafkaProducer

pool = None
producer = None

client = AsyncGroq(

    api_key= config("GROQ_API_KEY"),

)


async def run():
    # Establish a connection to the database
    global pool
    pool = await asyncpg.create_pool(
        host = config("DB_HOST"), 
        database=config("DB_NAME"), 
        user=config("DB_USER"), 
        password=config("DB_PASSWORD"),
        port=config("DB_PORT"),
        min_size=int(config("DB_POOL_MIN_SIZE")),
        max_size=int(config("DB_POOL_MAX_SIZE"))
    )

    global producer
    producer = AIOKafkaProducer(
        bootstrap_servers='kafka1:9092,kafka2:9092',
        value_serializer=lambda v: json.dumps(v).encode('utf-8')
    )
    await producer.start()
    

def extract_text_from_pdf(pdf_bytes):
    text = ""
    with pdfplumber.open(pdf_bytes) as pdf:
        for page in pdf.pages:
            text += page.extract_text() + "\n"
    return text

async def parse(id, cv=None):
    global pool, producer
    if pool is None or producer is None:
        await run()
    session = get_session()
    pdf_text = ""
    if not cv:
        async with session.create_client(
            's3',
            endpoint_url=config("MINIO_ENDPOINT"),
            aws_access_key_id=config("MINIO_ACCESS_KEY"),
            aws_secret_access_key=config("MINIO_SECRET_KEY"),
        ) as minio_client:
            response = await minio_client.get_object(Bucket=config("MINIO_CV_BUCKET"), Key=f"{id}")
            pdf_bytes = io.BytesIO((await response.get("Body").read()))
    else:
        pdf_bytes = io.BytesIO(cv)
    pdf_text = extract_text_from_pdf(pdf_bytes)
	
    prompt = '''You are an AI assistant specialised in extracting structured information from resumes. Given the following resume text, extract key details in exactly this JSON format. Here's an example:

        {
        "contactInformation": {
            "name": "Full Name",
            "phone": "Phone Number",
            "email": "Email Address",
            "city": "City",
            "country": "Country"
        },
        "education": [
            {
            "degree": "Degree Name",
            "university": "University Name",
            "faculty": "Faculty Name",
            "start Year": "Start Year",
            "End Year": "End Year",
            "grade": "provided grade"
            }
        ],
        "workExperience": [
            {
            "title": "Job Title",
            "description": "Job Description",
            "company": "Company Name",
            "start date": "YYYY-MM-DD",
            "end date": "YYYY-MM-DD"
            }
        ],
        "skills": [
            "Skill 1",
            "Skill 2",
            "Skill 3"
        ],
        "spoken languages": [
            "Language 1",
            "Language 2"
        ]
        }

        ### **Instructions (Follow Strictly):**
        1. **Maintain the exact JSON structure with correct nesting**. This is a **top priority**.  
        2. **Extract all available information while ensuring accuracy**.  
        3. **Format Dates**: 
            -Use `YYYY-MM-DD` format for both start and end dates in `workExperience` (e.g., `"2022-01-01"`).
            -If no day is found, extract the date in the format `YYYY-MM` in `workExperince` (e.g., `"2022-01"`).
            -If no month is found, extract the date in the format `YYYY` in `workExperince` (e.g., `"2022"`).
            -If the word `present` is written in the date return a string of the word "present".
            -If no date is found return an empty string.
        4. **Format City**: Use the city name in the "city" field. Write the city name only (e.g., "San Francisco").
        5. **Format Country**: Use the country name in the "country" field. Write the country name only (e.g., "USA").
        6. **Spoken Languages**: Include **only** the language names (e.g., `"English"`, `"Spanish"`).  
        7. **Skills**: 
            - Extract and list **all relevant skills**. Check for skills **in any section of the CV**, not just in a dedicated "Skills" section.
            - Ensure each individual skill is added as a separate element in the "skills" list.
            - No spoken languages should be included in the skills list.
        8. **Grade (GPA Handling)**:
        - If a GPA is provided, extract **only the numeric GPA** (e.g., `"3.48"` from `"GPA: 3.48 of 4.00"`).  
        - Do **not** include text like `"of 4.00"` or `"%"`.  
        - If the GPA is not explicitly available, check for a percentage (e.g., `"85%" → `"85"`).  
        - If no grade is found, leave it **blank**.  
        9. **Abbreviations**: Expand common abbreviations into their full form. Example:
        - `"ML"` → `"Machine Learning"`
        - `"NLP"` → `"Natural Language Processing"`
        - `"AI"` → `"Artificial Intelligence"`
        10. **If a field is missing, leave it blank** instead of guessing.  
        11. **Output only valid JSON**:  
        - **Do not include any introductory/explanatory text.**  
        - **Do not print `json` or any formatting hints before the JSON output.**
        12. **Phone Numbers**: If multiple phone numbers are found, include only the most relevant one (e.g., the primary number mentioned under contact details or the first valid number found). Ignore duplicates or secondary numbers.
        13. **Work Experience Description**: Include the description of the job in the "description" field in work experience. Leave an empty string if no description is mentioned.
        '''

    conversation =[
		{
			"role":"system",
			"content": prompt
		},
		{
			"role":"user",
			"content": f"Extract structured information from this resume:\n\n{pdf_text}"
		}
	]

    CV_query = await client.chat.completions.create(

		messages=conversation,

		model="llama-3.3-70b-versatile",

	)

    CV_response = CV_query.choices[0].message.content

    parsed_CV = re.search(r'```\s*(?:json)?\s*(.*?)```', CV_response, re.DOTALL)
    
    extracted_CV = None
    
    if parsed_CV:
        extracted_CV = parsed_CV.group(1).strip()
    else:
        extracted_CV = CV_response

    
    CV_dict = json.loads(extracted_CV)
    CV_dict["skills"] = [skill.lower() for skill in CV_dict.get("skills",[])]
    CV_dict["contactInformation"]["phone"] = CV_dict["contactInformation"]["phone"].replace(" ", "")
    
    async with pool.acquire() as conn:
        #insert new skills into the database
        await conn.execute("INSERT INTO Skills (name) SELECT unnest($1::text[]) ON CONFLICT (name) DO NOTHING;", CV_dict.get("skills", []))
        skills = await conn.fetch("SELECT id, name FROM Skills WHERE name = ANY($1);", CV_dict.get("skills", []))
        CV_dict["skills"] = [{"id":row["id"], "name": row["name"]} for row in skills]
        
	# handle grpc call
    if cv:
        return json.dumps(CV_dict)
    # handle consuming from kafka
    else:
        async with pool.acquire() as conn:
            await conn.fetch("INSERT INTO cv_keywords (cv_id, skills) VALUES($1, $2) ON CONFLICT (cv_id) DO NOTHING", int(id), [skill["name"] for skill in CV_dict.get("skills", [])])
        print("Parsed CV", flush=True)
        
        try:
            async with pool.acquire() as conn:
                res = await conn.fetch("SELECT user_id FROM cv WHERE id = $1", int(id))
                await producer.send_and_wait('cv_embedding_generation', value={"id": int(id), "userId": res[0]["user_id"]})
            print(f"Message successfully published to topic 'cv_embedding_generation' for id: {id}", flush=True)
        except Exception as e:
            print(f"Failed to publish message to Kafka: {e}", flush=True)


async def shutdown():
    global producer
    if producer is not None:
        await producer.stop()

