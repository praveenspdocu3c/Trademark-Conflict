from fileinput import filename
import time
import streamlit as st
import json
import openai, pandas as pd
import fitz  # PyMuPDF
from pydantic import BaseModel, Field, ValidationError
from typing import List, Dict, Union
import base64
from docx import Document  
from docx.shared import Pt
from io import BytesIO
import re

# Initialize AzureChatOpenAI
azure_endpoint = "https://chat-gpt-a1.openai.azure.com/"
api_key = "c09f91126e51468d88f57cb83a63ee36"
deployment_name = "DanielChatGPT16k"

openai.api_type = "azure"
openai.api_key = api_key
openai.api_base = azure_endpoint
openai.api_version = "2024-02-15-preview"

class TrademarkDetails(BaseModel):
    trademark_name: str = Field(description="The name of the Trademark", example="DISCOVER")
    status: str = Field(description="The Status of the Trademark", example="Registered")
    serial_number: str = Field(description="The Serial Number of the trademark from Chronology section", example="87−693,628")
    international_class_number: List[int] = Field(description="The International class number or Nice Classes number of the trademark from Goods/Services section or Nice Classes section", example=[18])
    owner: str = Field(description="The owner of the trademark", example="WALMART STORES INC")
    goods_services: str = Field(description="The goods/services from the document", example="LUGGAGE AND CARRYING BAGS; SUITCASES, TRUNKS, TRAVELLING BAGS, SLING BAGS FOR CARRYING INFANTS, SCHOOL BAGS; PURSES; WALLETS; RETAIL AND ONLINE RETAIL SERVICES")
    page_number: int = Field(description="The page number where the trademark details are found in the document", example=3)

def preprocess_text(text: str) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'[\u2013\u2014]', '-', text)
    return text

def is_correct_format_code1(page_text: str) -> bool:
    required_fields = ["Status:", "Goods/Services:", "Chronology:", "Last Reported Owner:"]
    return all(field in page_text for field in required_fields)

def is_correct_format_code2(page_text: str) -> bool:
    required_fields = ["Register", "Nice Classes", "Goods & Services"]
    return all(field in page_text for field in required_fields)

def extract_trademark_details_code1(document_chunk: str) -> Dict[str, Union[str, List[int]]]:
    response = openai.ChatCompletion.create(
        engine=deployment_name,
        messages=[
            {"role": "system", "content": "You are a helpful assistant for extracting Meta Data from the Trademark Document."},
            {"role": "user", "content": f"Extract the following details from the trademark document: trademark name, status, serial number, international class number, owner, goods & services, filed date, registration number.\n\nDocument:\n{document_chunk}"}
        ],
        max_tokens=4000,
        temperature=0
    )

    extracted_text = response.choices[0].message['content'].strip()
    details = {}
    for line in extracted_text.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            details[key.strip().lower().replace(" ", "_")] = value.strip()
    return details

def extract_trademark_details_code2(page_text: str) -> Dict[str, Union[str, List[int]]]:
    details = {}

    trademark_name_match = re.search(r"(?<=\n)([A-Za-z0-9'&!,\-. ]+)(?=\n)", page_text)
    details["trademark_name"] = trademark_name_match.group(1).strip() if trademark_name_match else ""

    status_match = re.search(r'Status\s*(?:\n|:\s*)([A-Za-z]+)', page_text, re.IGNORECASE)
    details["status"] = status_match.group(1).strip() if status_match else ""

    owner_match = re.search(r'Holder\s*(?:\n|:\s*)(.*)', page_text, re.IGNORECASE)
    if owner_match:
        details["owner"] = owner_match.group(1).strip()
    else:
        owner_match = re.search(r'Owner\s*(?:\n|:\s*)(.*)', page_text, re.IGNORECASE)
        details["owner"] = owner_match.group(1).strip() if owner_match else ""

    nice_classes_text = ""
    nice_classes_match = re.search(r'Nice Classes\s*(?:\n|:\s*)(\d+,\s*\d+(?:,\s*\d+)*)', page_text, re.IGNORECASE)

    if nice_classes_match:
        nice_classes_text = nice_classes_match.group(1)
        nice_classes = [int(cls) for cls in nice_classes_text.split(",")]
    else:
        single_class_match = re.search(r'Nice Classes\s*(?:\n|:\s*)(\d+)', page_text, re.IGNORECASE)
        if single_class_match:
            nice_classes_text = single_class_match.group(1)
            nice_classes = [int(nice_classes_text)]

    details["international_class_number"] = nice_classes

    serial_number_match = re.search(r'Registration#\s*(.*)', page_text, re.IGNORECASE)
    details["serial_number"] = serial_number_match.group(1).strip() if serial_number_match else ""

    goods_services_match = re.search(r'Goods & Services\s*(.*?)(?=\s*G&S translation|$)', page_text, re.IGNORECASE | re.DOTALL)
    details["goods_services"] = goods_services_match.group(1).strip() if goods_services_match else ""

    return details

def read_pdf(file_path: str, exclude_header_footer: bool = True) -> str:
    document_text = ""
    with fitz.open(file_path) as pdf_document:
        for page_num in range(pdf_document.page_count):
            page = pdf_document.load_page(page_num)
            if exclude_header_footer:
                rect = page.rect
                x0 = rect.x0
                y0 = rect.y0 + rect.height * 0.1
                x1 = rect.x1
                y1 = rect.y1 - rect.height * 0.1
                page_text = page.get_text("text", clip=(x0, y0, x1, y1))
            else:
                page_text = page.get_text()
            document_text += page_text
    return document_text

def split_text(text: str, max_tokens: int = 1500) -> List[str]:
    chunks = []
    current_chunk = []
    current_length = 0

    for line in text.split('\n'):
        line_length = len(line.split())
        if current_length + line_length > max_tokens:
            chunks.append('\n'.join(current_chunk))
            current_chunk = [line]
            current_length = line_length
        else:
            current_chunk.append(line)
            current_length += line_length

    if current_chunk:
        chunks.append('\n'.join(current_chunk))

    return chunks

def parse_international_class_numbers(class_numbers: str) -> List[int]:
    numbers = class_numbers.split(',')
    return [int(num.strip()) for num in numbers if num.strip().isdigit()]

def parse_trademark_details(document_path: str) -> List[Dict[str, Union[str, List[int]]]]:
    with fitz.open(document_path) as pdf_document:
        all_extracted_data = []
        for page_num in range(pdf_document.page_count):
            page = pdf_document.load_page(page_num)
            page_text = page.get_text()
            
            if is_correct_format_code1(page_text):
                preprocessed_chunk = preprocess_text(page_text)
                extracted_data = extract_trademark_details_code1(preprocessed_chunk)
                additional_data = extract_international_class_numbers_and_goods_services(page_text)
                
                if extracted_data:
                    extracted_data["page_number"] = page_num + 1
                    extracted_data.update(additional_data)
                    all_extracted_data.append(extracted_data)
                    
                trademark_list = []
                for i, data in enumerate(all_extracted_data, start=1):
                    try:
                        trademark_name = data.get("trademark_name", "").split(',')[0].strip()
                        if "Global Filings" in trademark_name:
                            trademark_name = trademark_name.split("Global Filings")[0].strip()
                        owner = data.get("owner", "").split(',')[0].strip()
                        status = data.get("status", "").split(',')[0].strip()
                        serial_number = data.get("serial_number", "")
                        international_class_number = data.get("international_class_numbers", [])
                        goods_services = data.get("goods_services", "")
                        page_number = data.get("page_number", "")

                        # If crucial fields are missing, attempt to re-extract the values
                        if not trademark_name or not owner or not status or not international_class_number:
                            preprocessed_chunk = preprocess_text(data.get("raw_text", ""))
                            extracted_data = extract_trademark_details_code1(preprocessed_chunk)
                            trademark_name = extracted_data.get("trademark_name", trademark_name).split(',')[0].strip()
                            if "Global Filings" in trademark_name:
                                trademark_name = trademark_name.split("Global Filings")[0].strip()
                            owner = extracted_data.get("owner", owner).split(',')[0].strip()
                            status = extracted_data.get("status", status).split(',')[0].strip()
                            international_class_number = parse_international_class_numbers(extracted_data.get("international_class_number", "")) or international_class_number

                        trademark_details = TrademarkDetails(
                            trademark_name=trademark_name,
                            owner=owner,
                            status=status,
                            serial_number=serial_number,
                            international_class_number=international_class_number,
                            goods_services=goods_services,
                            page_number=page_number
                        )
                        trademark_info = {
                            "trademark_name": trademark_details.trademark_name,
                            "owner": trademark_details.owner,
                            "status": trademark_details.status,
                            "serial_number": trademark_details.serial_number,
                            "international_class_number": trademark_details.international_class_number,
                            "goods_services": trademark_details.goods_services,
                            "page_number": trademark_details.page_number,
                        }
                        trademark_list.append(trademark_info)
                    except ValidationError as e:
                        print(f"Validation error for trademark {i}: {e}")
                                    
            else :
                if not is_correct_format_code2(page_text):
                    continue

                extracted_data = extract_trademark_details_code2(page_text)
                if extracted_data:
                    extracted_data["page_number"] = page_num + 1
                    all_extracted_data.append(extracted_data)

                trademark_list = []
                for i, data in enumerate(all_extracted_data, start=1):
                    try:
                        trademark_details = TrademarkDetails(
                            trademark_name=data.get("trademark_name", ""),
                            owner=data.get("owner", ""),
                            status=data.get("status", ""),
                            serial_number=data.get("serial_number", ""),
                            international_class_number=data.get("international_class_number", []),
                            goods_services=data.get("goods_services", ""),
                            page_number=data.get("page_number", 0)
                        )
                        if (trademark_details.trademark_name != "" and trademark_details.owner != "" and trademark_details.status != "" and trademark_details.goods_services != ""):
                                trademark_info = {
                                    "trademark_name": trademark_details.trademark_name,
                                    "owner": trademark_details.owner,
                                    "status": trademark_details.status,
                                    "serial_number": trademark_details.serial_number,
                                    "international_class_number": trademark_details.international_class_number,
                                    "goods_services": trademark_details.goods_services,
                                    "page_number": trademark_details.page_number,
                                }
                                trademark_list.append(trademark_info)
                    except ValidationError as e:
                        print(f"Validation error for trademark {i}: {e}")

        return trademark_list

    
def extract_international_class_numbers_and_goods_services(document: str) -> Dict[str, Union[List[int], str]]:
    """ Extract the International Class Numbers and Goods/Services from the document """
    class_numbers = []
    goods_services = []
    pattern = r'International Class (\d+): (.*?)(?=\nInternational Class \d+:|\n[A-Z][a-z]+:|\nLast Reported Owner:|\Z)'
    matches = re.findall(pattern, document, re.DOTALL)
    for match in matches:
        class_number = int(match[0])
        class_numbers.append(class_number)
        goods_services.append(f"Class {class_number}: {match[1].strip()}")
    return {
        "international_class_numbers": class_numbers,
        "goods_services": "\n".join(goods_services)
    }

def compare_trademarks(existing_trademark: List[Dict[str, Union[str, List[int]]]], proposed_name: str, proposed_class: str, proposed_goods_services: str) -> List[Dict[str, int]]:
    proposed_classes = [int(c.strip()) for c in proposed_class.split(',')]
    response_reasoning = openai.ChatCompletion.create(
        engine=deployment_name,
        messages=[
            {"role": "system", "content": """You are a trademark attorney to properly reasoning based on given conditions and assign conflict grade high or moderate or low to existing trademark and respond with only High or Moderate or Low. \n\n 
                                            Conditions for determining Conflict Grades:\n\n 
                                            
                                            Condition 1: Trademark Name Comparison\n 
                                            - Condition 1A: The existing trademark's name is a character-for-character match with the proposed trademark name.\n 
                                            - Condition 1B: The existing trademark's name is semantically equivalent to the proposed trademark name.\n 
                                            - Condition 1C: The existing trademark's name is phonetically equivalent to the proposed trademark name.\n 
                                            - Condition 1D: Primary Position Requirement- In the context of trademark conflicts, the primary position of a trademark refers to the first word or phrase element in a multi-word or phrase trademark. For a conflict to exist between an existing trademark and a proposed trademark based on Condition 1D, the proposed trademark name must be in the primary position of the existing trademark. This means that the proposed trademark name should be the first word of the existing trademark.\n
                                                            Example:\n Existing Trademark: "STORIES AND JOURNEYS"\n Proposed Trademark: "JOURNEY"\n Analysis:\n The existing trademark "STORIES AND JOURNEYS" consists of multiple words/phrases.\n For the proposed trademark "JOURNEY" to be in conflict under Condition 1D, it must appear as the first word/phrase in the existing trademark.\n In this case, the first word/phrase in "STORIES AND JOURNEYS" is "STORIES", not "JOURNEY".\n Therefore, "JOURNEY" does not meet Condition 1D because it is not in the primary position of the existing trademark.\n
                                                            Example:\n Existing Trademark: "JOURNEY BY COMPANION"\n Proposed Trademark: "JOURNEY"\n Analysis:\n The existing trademark "JOURNEY BY COMPANION" consists of multiple words/phrases.\n For the proposed trademark "JOURNEY" to be in conflict under Condition 1D, it must appear as the first word/phrase in the existing trademark.\n In this case, the first word/phrase in "JOURNEY BY COMPANION" is "JOURNEY".\n Therefore, "JOURNEY" does meet Condition 1D because it is in the primary position of the existing trademark.\n
                                                                                                        
                                            Condition 2: Goods/Services Classification\n 
                                            - Condition 2: The existing trademark's goods/services are in the same class as those of the proposed trademark.\n
                                            
                                            Condition 3: Target Market and Products\n 
                                            - Condition 3A: The existing trademark's goods/services target the exact same products as the proposed trademark.\n 
                                            - Condition 3B: The existing trademark's goods/services target an exact or similar market as the proposed trademark.\n
                                            
                                            If existing trademark in user given input satisfies:\n\n
                                            - Special case: If existing Trademark Status is Cancelled or Abandoned, they will automatically be considered as conflict grade low but still give the reasoning for the potential conflicts.\n\n
                                            - If the existing trademark satisfies Condition 1A, 1B, or 1C, and also satisfies the revised Condition 1D (when applicable), along with Condition 2, and both Condition 3A and 3B, then the conflict grade should be High.
                                            - If the existing trademark satisfies any two of the following: Condition 1A, 1B, or 1C (with the revised Condition 1D being a necessary component for these to be considered satisfied when applicable), Condition 2, Condition 3A and 3B, then the conflict grade should be Moderate.
                                            - If the existing trademark satisfies only one (or none) of the conditions: Condition 1A, 1B, 1C (only if the revised Condition 1D is also satisfied when applicable), Condition 2, Condition 3A and 3B, then the conflict grade should be Low.\n\n
                                            
                                            Format of the Response:\n
                                            Resoning for Conflict: Reasoning for conflict in bullet points (In reasoning, if exact same goods, services and industries: list the overlaps, you should reasoning whether the good/services are overlapping or not including classes (if same as proposed trademark or not) and see trademark name whether identical (character-for-character) matches, phonetic equivalents, if it is in primary position (first word in the phrase) or not, if it is not in primary position (first word in the phrase) of the existing trademark it is not conflicting and standard plural forms for subject goods and goods that may be related or not. Reasoning should be based on provided information. Do not provide any kind of hypothetical reasoning.)\n\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "JOURNEY TO THE EDGE" contains the word "JOURNEY," which is a character-for-character match with the proposed trademark "JOURNEY" (Condition 1A satisfied).\n - The word "JOURNEY" is in the primary position in the existing trademark "JOURNEY TO THE EDGE," satisfying the revised Condition 1D.\n - The existing trademark is in International Class 18, which is the same class as the proposed trademark for luggage and carrying bags, satisfying Condition 2.\n - The goods/services of the existing trademark are related to carrying bags, which overlap with the goods/services of the proposed trademark, satisfying Condition 3A.\n - Both trademarks target the same market of consumers who are in need of carrying bags and related products, satisfying Condition 3B.\n Conflict Grade: High\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "JOURNEYMAN" contains the word "JOURNEY," but it is not a character-for-character match with the proposed trademark "JOURNEY" (Condition 1A not satisfied).\n - The term "JOURNEY" within "JOURNEYMAN" is not semantically equivalent to the standalone proposed trademark "JOURNEY" (Condition 1B not satisfied).\n - The term "JOURNEY" within "JOURNEYMAN" may be phonetically similar to the proposed trademark "JOURNEY," but as part of a compound word, it does not create a phonetic match for the entire proposed trademark (Condition 1C not satisfied).\n - The existing trademark and the proposed trademark are in the same International Class 18, which includes luggage and carrying bags (Condition 2 satisfied).\n - The existing trademark's goods/services include backpacks, which fall under the broader category of carrying bags, overlapping with the proposed trademark's goods/services (Condition 3A satisfied).\n - Both trademarks target the same market of consumers interested in carrying bags and luggage (Condition 3B satisfied).\n Conflict Grade: Moderate\n 
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "JOURNEY MARKET" contains the term "JOURNEY," which is a character-for-character match with the proposed trademark "JOURNEY" (Condition 1A satisfied).\n - The term "JOURNEY" in the existing trademark is in the primary position, satisfying the revised Condition 1D.\n - The existing trademark and the proposed trademark share the same International Class 35 for retail services, satisfying Condition 2.\n - The existing trademark's goods/services under Class 35 overlap with the proposed trademark's retail and online retail services, satisfying Condition 3B. However, the goods under Class 24 of the existing trademark do not overlap with the Class 18 goods of the proposed trademark, so Condition 3A is not satisfied.\n Conflict Grade: Moderate\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "JOURNEY" is a character-for-character match with the proposed trademark "JOURNEY" (Condition 1A satisfied).\n - The term "JOURNEY" is in the primary position in the existing trademark, satisfying the revised Condition 1D.\n - The existing trademark and the proposed trademark share International Class 35 for retail and online retail services, satisfying Condition 2.\n - The existing trademark's goods/services under Class 35 overlap with the proposed trademark's retail and online retail services, satisfying Condition 3B. However, the goods under Class 9, 36, 37, 38, and 39 of the existing trademark do not overlap with the Class 18 goods of the proposed trademark, so Condition 3A is not satisfied.\n Conflict Grade: Moderate\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "DISCOVERY ZONE" contains the term "DISCOVERY," which is not a character-for-character match with the proposed trademark "DISCOVER" (Condition 1A not satisfied).\n - The term "DISCOVERY" is not semantically equivalent to the standalone proposed trademark "DISCOVER" (Condition 1B not satisfied).\n - The term "DISCOVERY" is phonetically similar to "DISCOVER," but not equivalent, especially given the additional syllable "-Y" at the end of "DISCOVERY" (Condition 1C not satisfied).\n - The term "DISCOVER" is not in the primary position in the existing trademark "DISCOVERY ZONE" (Condition 1D not satisfied).\n - The existing trademark is registered under International Class 18, which is the same class as the proposed trademark for luggage and carrying bags, satisfying Condition 2.\n - The existing trademark's goods/services in Class 18 include suitcases and bags, which are similar to the proposed trademark's goods/services, satisfying Condition 3A.\n - The existing trademark's goods/services target a similar market as the proposed trademark, given that both are involved in the sale of bags and accessories, satisfying Condition 3B.\n Conflict Grade: Moderate\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "DISCOVER THE BEST" contains the term "DISCOVER," which is a character-for-character match with the proposed trademark "DISCOVER" (Condition 1A satisfied).\n - The term "DISCOVER" is in the primary position in the existing trademark "DISCOVER THE BEST," satisfying the revised Condition 1D.\n - The existing trademark is registered under International Class 35, which is the same class as the proposed trademark for retail and online retail services, satisfying Condition 2.\n - The existing trademark's goods/services include "travel bag," which overlaps with the proposed trademark's goods of "luggage and carrying bags," satisfying Condition 3A.\n - Both trademarks target a similar market as they both offer online retail services, satisfying Condition 3B.\n Conflict Grade: High\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "V VIBEXCHANGE DISCOVER GREAT BEERS, MEET GREAT PEOPLE." contains the term "DISCOVER," but it is not a character-for-character match with the proposed trademark "DISCOVER" (Condition 1A not satisfied).\n - The term "DISCOVER" in the existing trademark is not semantically equivalent to the standalone proposed trademark "DISCOVER" (Condition 1B not satisfied).\n - The term "DISCOVER" in the existing trademark may be phonetically equivalent when spoken in isolation, but as part of a longer phrase, it does not create a phonetic match for the entire proposed trademark "DISCOVER" (Condition 1C not satisfied).\n - The term "DISCOVER" is not in the primary position in the existing trademark phrase, so the revised Condition 1D is not satisfied.\n - The existing trademark is in Class 35, which is the same class as the proposed trademark for retail and online retail services, satisfying Condition 2.\n - The existing trademark's goods/services do not target the exact same products as the proposed trademark, as it does not specifically mention luggage or carrying bags, but it does include bags in a broader sense (Condition 3A not fully satisfied).\n - The existing trademark's goods/services target a similar market as the proposed trademark, given that both are involved in retail services, satisfying Condition 3B.\n Conflict Grade: Low\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "VIZICAINE" is not a character-for-character match with the proposed trademark "Visiquan" (Condition 1A not satisfied).\n - The existing trademark "VIZICAINE" is not semantically equivalent to the proposed trademark "Visiquan" (Condition 1B not satisfied).\n - The existing trademark "VIZICAINE" is phonetically similar to "Visiquan," which could lead to confusion (Condition 1C satisfied).\n - The proposed trademark "Visiquan" does not match the existing trademark in the primary position of a phrase (Condition 1D not applicable as neither trademark is a phrase).\n - Both trademarks are in the same Nice Class 5 for pharmaceutical compositions for ophthalmological use (Condition 2 satisfied).\n - The existing trademark's goods/services in Class 5 are for a pharmaceutical composition for ophthalmological use, which is similar to the proposed trademark's goods/services of ophthalmic drops to treat corneal ulcers in animals, satisfying Condition 3A.\n - Both trademarks target the pharmaceutical market with a focus on ophthalmology, satisfying Condition 3B.\n Conflict Grade: High\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "VIMIAN" is not a character-for-character match with the proposed trademark "Visiquan" (Condition 1A not satisfied).\n - The existing trademark "VIMIAN" is not semantically equivalent to the proposed trademark "Visiquan" (Condition 1B not satisfied).\n - The existing trademark "VIMIAN" is not phonetically equivalent to the proposed trademark "Visiquan" (Condition 1C not satisfied).\n - The proposed trademark "Visiquan" does not match the existing trademark in the primary position of a phrase (Condition 1D not applicable as neither trademark is a phrase).\n - Both trademarks are in the same International Class 5 for pharmaceutical and veterinary preparations (Condition 2 satisfied).\n - The existing trademark's goods/services in Class 5 include medicated preparations for the care of eyes and ears, which is similar to the proposed trademark's goods/services of ophthalmic drops to treat corneal ulcers in animals, satisfying Condition 3A.\n - Both trademarks target the veterinary market with a focus on medical and pharmaceutical preparations for animals, satisfying Condition 3B.\n Conflict Grade: Moderate\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "UNITED " is not a character-for-character match with the proposed trademark "UNITED BY SNAPDRAGON" (Condition 1A not satisfied).\n - The existing trademark "UNITED" is not semantically equivalent to the proposed trademark "UNITED BY SNAPDRAGON" (Condition 1B not satisfied).\n - The existing trademark "UNITED" is not phonetically equivalent to the proposed trademark "UNITED BY SNAPDRAGON" (Condition 1C not satisfied).\n - The proposed trademark "UNITED BY SNAPDRAGON" does not meet the conditions for trademark registration. Specifically, the term "UNITED" is in the primary position, but the proposed trademark is not a single word, and the existing trademark does not contain all the words of the proposed trademark. Therefore, (Condition 1D is not satisfied).\n - The existing trademark and the proposed trademark share the same International Class Numbers 9, 38, and 42, satisfying Condition 2.\n - The existing trademark's goods/services include type face fonts recorded on magnetic and optical media, which are related to the proposed trademark's goods/services of computer hardware and software, integrated circuits, and related products, satisfying Condition 3A.\n - Both trademarks target a similar market of consumers interested in computer hardware, software, and related technologies, satisfying Condition 3B.\n Conflict Grade: Moderate\n
                                            - Example :\n Reasoning for Conflict:\n - The existing trademark "REFRESHERY" is not a character-for-character match with the proposed trademark "REFRESHERS" (Condition 1A not satisfied).\n - The existing trademark "REFRESHERY" is not semantically equivalent to the proposed trademark "REFRESHERS" (Condition 1B not satisfied).\n - The existing trademark "REFRESHERY" is phonetically similar to "REFRESHERS," which could lead to confusion (Condition 1C satisfied).\n - The proposed trademark "REFRESHERS" does not match the existing trademark in the primary position of a phrase (Condition 1D not applicable as neither trademark is a phrase).\n - Both trademarks are in the same International Class 30 for beverages, satisfying Condition 2.\n - The existing trademark's goods/services in Class 30 include tea and tea-based beverages, which overlap with the proposed trademark's goods/services of iced tea, satisfying Condition 3A.\n - Both trademarks target a similar market of consumers interested in tea and tea-based beverages, satisfying Condition 3B.\n Conflict Grade: High\n
                                            
                                            Conflict Grade: Based on above reasoning (Low or Moderate or High)."""
                                            },
            {"role": "user", "content": f"""Compare the following existing and proposed trademarks and determine the conflict grade.\n
                                            Existing Trademark:\n
                                            Name: {existing_trademark['trademark_name']}\n
                                            Goods/Services: {existing_trademark['goods_services']}\n
                                            International Class Numbers: {existing_trademark['international_class_number']}\n
                                            Status: {existing_trademark['status']}\n
                                            Owner: {existing_trademark['owner']}\n
                                            Proposed Trademark:\n
                                            Name: {proposed_name}\n 
                                            Goods/Services: {proposed_goods_services}\n
                                            International Class Numbers: {proposed_classes}\n"""
            }
        ],
        max_tokens=2000,
        temperature=0,
        top_p = 1
    )
    reasoning = response_reasoning.choices[0].message['content'].strip()
    conflict_grade = reasoning.split("Conflict Grade:", 1)[1].strip() 
    progress_bar.progress(70)

    return {
        'Trademark name': existing_trademark['trademark_name'],
        'Trademark status': existing_trademark['status'],
        'Trademark owner': existing_trademark['owner'],
        'Trademark class Number': existing_trademark['international_class_number'],
        'conflict_grade': conflict_grade,
        'reasoning': reasoning
    }


def extract_proposed_trademark_details(file_path: str) -> Dict[str, Union[str, List[int]]]:
    """ Extract proposed trademark details from the given input format """
    proposed_details = {}
    with fitz.open(file_path) as pdf_document:
        if pdf_document.page_count > 0:
            page = pdf_document.load_page(0)
            page_text = preprocess_text(page.get_text())
            
    name_match = re.search(r'Mark Searched:\s*(.*?)(?=\s*Client Name:)', page_text, re.IGNORECASE | re.DOTALL)
    if name_match:
        proposed_details["proposed_trademark_name"] = name_match.group(1).strip()

    goods_services_match = re.search(r'Goods/Services:\s*(.*?)(?=\s*Trademark Research Report)', page_text, re.IGNORECASE | re.DOTALL)
    if goods_services_match:
        proposed_details["proposed_goods_services"] = goods_services_match.group(1).strip()
    
    # Use LLM to find the international class number based on goods & services
    if "proposed_goods_services" in proposed_details:
        goods_services = proposed_details["proposed_goods_services"]
        class_numbers = find_class_numbers(goods_services)
        proposed_details["proposed_nice_classes_number"] = class_numbers
    
    return proposed_details

def find_class_numbers(goods_services: str) -> List[int]:
    """ Use LLM to find the international class numbers based on goods & services """
        # Initialize AzureChatOpenAI
    azure_endpoint = "https://chat-gpt-a1.openai.azure.com/"
    api_key = "c09f91126e51468d88f57cb83a63ee36"
    deployment_name = "DanielChatGPT16k"

    openai.api_type = "azure"
    openai.api_key = api_key
    openai.api_base = azure_endpoint
    openai.api_version = "2024-02-15-preview"
    
    response = openai.ChatCompletion.create(
        engine=deployment_name,
        messages=[
            {"role": "system", "content": "You are a helpful assistant for finding the International class number of provided Goods & Services."},
            {"role": "user", "content": f"The goods/services are: {goods_services}. Find the international class numbers."}
        ],
        max_tokens=150,
        temperature=0
    )
    class_numbers_str = response.choices[0].message['content'].strip()
    
    # Extracting class numbers and removing duplicates
    class_numbers = re.findall(r'(?<!\d)\d{2}(?!\d)', class_numbers_str)  # Look for two-digit numbers
    class_numbers = ','.join(set(class_numbers))  # Convert to set to remove duplicates, then join into a single string
    
    return class_numbers

def extract_proposed_trademark_details2(file_path: str) -> Dict[str, Union[str, List[int]]]:
    """ Extract proposed trademark details from the first page of the document """
    proposed_details = {}
    with fitz.open(file_path) as pdf_document:
        if pdf_document.page_count > 0:
            page = pdf_document.load_page(0)
            page_text = preprocess_text(page.get_text())
            
            name_match = re.search(r'Name:\s*(.*?)(?=\s*Nice Classes:)', page_text)
            if name_match:
                proposed_details["proposed_trademark_name"] = name_match.group(1).strip()
                
            nice_classes_match = re.search(r'Nice Classes:\s*(\d+(?:,\s*\d+)*)', page_text)
            if nice_classes_match:
                proposed_details["proposed_nice_classes_number"] = nice_classes_match.group(1).strip()
            
            goods_services_match = re.search(r'Goods & Services:\s*(.*?)(?=\s*Registers|$)', page_text, re.IGNORECASE | re.DOTALL)
            if goods_services_match:
                proposed_details["proposed_goods_services"] = goods_services_match.group(1).strip()
    
    return proposed_details

# Streamlit App  
st.title("Trademark Document Parser")  
  
# File upload  
uploaded_files = st.sidebar.file_uploader("Choose PDF files", type="pdf", accept_multiple_files=True)  
  
if uploaded_files:  
    if st.sidebar.button("Check Conflicts", key="check_conflicts"):  
        total_files = len(uploaded_files)  
        progress_bar = st.progress(0)  
        for i, uploaded_file in enumerate(uploaded_files):  
            # Save uploaded file to a temporary file path  
            temp_file_path = f"temp_{uploaded_file.name}"  
            with open(temp_file_path, "wb") as f:  
                f.write(uploaded_file.read())  
            
            proposed_trademark_details = extract_proposed_trademark_details(temp_file_path)  
                            
            if proposed_trademark_details:  
                proposed_name = proposed_trademark_details.get('proposed_trademark_name', '')  
                proposed_class = proposed_trademark_details.get('proposed_nice_classes_number')  
                proposed_goods_services = proposed_trademark_details.get('proposed_goods_services', '')  
                with st.expander(f"Proposed Trademark Details for {uploaded_file.name}"):  
                        st.write(f"Proposed Trademark name: {proposed_name}")  
                        st.write(f"Proposed class-number: {proposed_class}")  
                        st.write(f"Proposed Goods & Services: {proposed_goods_services}")  
            else:  
                
                proposed_trademark_details = extract_proposed_trademark_details2(temp_file_path)  
                
                if proposed_trademark_details:  
                    proposed_name = proposed_trademark_details.get('proposed_trademark_name', '')  
                    proposed_class = proposed_trademark_details.get('proposed_nice_classes_number')  
                    proposed_goods_services = proposed_trademark_details.get('proposed_goods_services', '')  
                    with st.expander(f"Proposed Trademark Details for {uploaded_file.name}"):  
                        st.write(f"Proposed Trademark name: {proposed_name}")  
                        st.write(f"Proposed class-number: {proposed_class}")  
                        st.write(f"Proposed Goods & Services: {proposed_goods_services}")  
                else:  
                    st.error(f"Unable to extract Proposed Trademark Details for {uploaded_file.name}")  
                    continue  
            for i in range(1,21):
                time.sleep(0.5)
                progress_bar.progress(i)
                
            progress_bar.progress(25)
            # Initialize AzureChatOpenAI
            azure_endpoint = "https://chat-gpt-a1.openai.azure.com/"
            api_key = "c09f91126e51468d88f57cb83a63ee36"
            deployment_name = "DanielChatGPT16k"

            openai.api_type = "azure"
            openai.api_key = api_key
            openai.api_base = azure_endpoint
            openai.api_version = "2024-02-15-preview"
            
            existing_trademarks = parse_trademark_details(temp_file_path)
            for i in range(25,46):
                time.sleep(0.5)
                progress_bar.progress(i)  
                
            progress_bar.progress(50)
            st.success(f"Existing Trademarks Data Extracted Successfully for {uploaded_file.name}!")  
            # Display extracted details              
            azure_endpoint = "https://danielingitaraj.openai.azure.com/"
            api_key = "a5c4e09a50dd4e13a69e7ef19d07b48c"
            deployment_name = "GPT4"  

            openai.api_type = "azure"
            openai.api_key = api_key
            openai.api_base = azure_endpoint 
            openai.api_version = "2024-02-01"

            high_conflicts = []
            moderate_conflicts = []
            low_conflicts = []

            for existing_trademark in existing_trademarks:  
                conflict = compare_trademarks(existing_trademark, proposed_name, proposed_class, proposed_goods_services)  
                if conflict['conflict_grade'] == "High":  
                    high_conflicts.append(conflict)  
                elif conflict['conflict_grade'] == "Moderate":  
                    moderate_conflicts.append(conflict)  
                else:  
                    low_conflicts.append(conflict)  

            st.sidebar.write("_________________________________________________")
            st.sidebar.subheader("\n\nConflict Grades : \n")  
            st.sidebar.markdown(f"File: {proposed_name}")  
            st.sidebar.markdown(f"Total number of conflicts: {len(high_conflicts) + len(moderate_conflicts) + len(low_conflicts)}")
            st.sidebar.markdown(f"High Conflicts: {len(high_conflicts)}")  
            st.sidebar.markdown(f"Moderate Conflicts: {len(moderate_conflicts)}")  
            st.sidebar.markdown(f"Low Conflicts: {len(low_conflicts)}")  
            st.sidebar.write("_________________________________________________")
  
            document = Document()  
            
            document.add_heading(f'Trademark Conflict List for {proposed_name} :')            
            document.add_paragraph(f"\n\nTotal number of conflicts: {len(high_conflicts) + len(moderate_conflicts) + len(low_conflicts)}\n- High Conflicts: {len(high_conflicts)}\n- Moderate Conflicts: {len(moderate_conflicts)}\n- Low Conflicts: {len(low_conflicts)}\n")  
              
            if len(high_conflicts) > 0:  
                        document.add_heading('Trademarks with High Conflicts:', level=2)  
                        # Create a pandas DataFrame from the JSON list    
                        df_high = pd.DataFrame(high_conflicts) 
                        df_high = df_high.drop(columns=['reasoning'])  
                        # Create a table in the Word document    
                        table_high = document.add_table(df_high.shape[0] + 1, df_high.shape[1])
                        # Set a predefined table style (with borders)  
                        table_high.style = 'TableGrid'  # This is a built-in style that includes borders  
                        # Add the column names to the table    
                        for i, column_name in enumerate(df_high.columns):  
                            table_high.cell(0, i).text = column_name  
                        # Add the data to the table    
                        for i, row in df_high.iterrows():  
                            for j, value in enumerate(row):  
                                table_high.cell(i + 1, j).text = str(value)

            if len(moderate_conflicts) > 0:  
                        document.add_heading('Trademarks with Moderate Conflicts:', level=2)  
                        # Create a pandas DataFrame from the JSON list    
                        df_moderate = pd.DataFrame(moderate_conflicts)
                        df_moderate = df_moderate.drop(columns=['reasoning'])  
                        # Create a table in the Word document    
                        table_moderate = document.add_table(df_moderate.shape[0] + 1, df_moderate.shape[1])
                        # Set a predefined table style (with borders)  
                        table_moderate.style = 'TableGrid'  # This is a built-in style that includes borders  
                        # Add the column names to the table    
                        for i, column_name in enumerate(df_moderate.columns):  
                            table_moderate.cell(0, i).text = column_name  
                        # Add the data to the table    
                        for i, row in df_moderate.iterrows():  
                            for j, value in enumerate(row):  
                                table_moderate.cell(i + 1, j).text = str(value)

            if len(low_conflicts) > 0:  
                        document.add_heading('Trademarks with Low Conflicts:', level=2)  
                        # Create a pandas DataFrame from the JSON list    
                        df_low = pd.DataFrame(low_conflicts)  
                        df_low = df_low.drop(columns=['reasoning'])
                        # Create a table in the Word document    
                        table_low = document.add_table(df_low.shape[0] + 1, df_low.shape[1])
                        # Set a predefined table style (with borders)  
                        table_low.style = 'TableGrid'  # This is a built-in style that includes borders  
                        # Add the column names to the table    
                        for i, column_name in enumerate(df_low.columns):  
                            table_low.cell(0, i).text = column_name  
                        # Add the data to the table    
                        for i, row in df_low.iterrows():  
                            for j, value in enumerate(row):  
                                table_low.cell(i + 1, j).text = str(value)
                        
            def add_conflict_paragraph(document, conflict):  
                p = document.add_paragraph(f"Trademark Name : {conflict.get('Trademark name', 'N/A')}")  
                p.paragraph_format.line_spacing = Pt(18)  
                p.paragraph_format.space_after = Pt(0)
                p = document.add_paragraph(f"Trademark Status : {conflict.get('Trademark status', 'N/A')}")  
                p.paragraph_format.line_spacing = Pt(18)  
                p.paragraph_format.space_after = Pt(0)
                p = document.add_paragraph(f"Trademark Owner : {conflict.get('Trademark owner', 'N/A')}")  
                p.paragraph_format.line_spacing = Pt(18)  
                p.paragraph_format.space_after = Pt(0)
                p = document.add_paragraph(f"Trademark Class Number : {conflict.get('Trademark class Number', 'N/A')}")  
                p.paragraph_format.line_spacing = Pt(18)
                p.paragraph_format.space_after = Pt(0)  
                p = document.add_paragraph(" ")  
                p.paragraph_format.line_spacing = Pt(18)  
                p.paragraph_format.space_after = Pt(0) 
                p = document.add_paragraph(f"{conflict.get('reasoning','N/A')}\n")  
                p.paragraph_format.line_spacing = Pt(18)  
                p = document.add_paragraph(" ")  
                p.paragraph_format.line_spacing = Pt(18)  
            
            if len(high_conflicts) > 0:  
                document.add_heading('Trademarks with High Conflicts Reasoning:', level=2)  
                p = document.add_paragraph(" ")  
                p.paragraph_format.line_spacing = Pt(18)  
                for conflict in high_conflicts:  
                    add_conflict_paragraph(document, conflict)  
            
            if len(moderate_conflicts) > 0:  
                document.add_heading('Trademarks with Moderate Conflicts Reasoning:', level=2)  
                p = document.add_paragraph(" ")  
                p.paragraph_format.line_spacing = Pt(18)  
                for conflict in moderate_conflicts:  
                    add_conflict_paragraph(document, conflict)  
            
            if len(low_conflicts) > 0:  
                document.add_heading('Trademarks with Low Conflicts Reasoning:', level=2)  
                p = document.add_paragraph(" ")  
                p.paragraph_format.line_spacing = Pt(18)  
                for conflict in low_conflicts:  
                    add_conflict_paragraph(document, conflict)  
                    
            for i in range(70,96):
                time.sleep(0.5)
                progress_bar.progress(i)  
                
            progress_bar.progress(100)
  
            filename = proposed_name
            doc_stream = BytesIO()  
            document.save(doc_stream)  
            doc_stream.seek(0)  
            download_table = f'<a href="data:application/octet-stream;base64,{base64.b64encode(doc_stream.read()).decode()}" download="{filename + " Trademark Conflict Report"}.docx">Download Trademark Conflicts Report:{filename}</a>'  
            st.sidebar.markdown(download_table, unsafe_allow_html=True)  
            st.success(f"{proposed_name} Document conflict report successfully completed!")
            st.write("______________________________________________________________________________________________________________________________")
  
        progress_bar.progress(100)
        st.success("All documents processed successfully!")  
