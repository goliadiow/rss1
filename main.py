import os
import re
import json
import time
import html
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
import google.generativeai as genai

# 1. Initialize API Keys and SEC Headers
# CRITICAL: Replace with your actual name and email to comply with SEC rules
SEC_HEADERS = {'User-Agent': 'ErichRiesenberg itserich@gmail.com'}

api_key = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-2.5-flash')

SEARCH_TERMS = 'company' 
FORM_TYPES = '8-K'

# 2. Read Local Memory Files
DATABASE_FILE = 'adsh_db.json'
MARKER_FILE = 'high_water_mark.txt'

adsh_db = {}
if os.path.exists(DATABASE_FILE):
    with open(DATABASE_FILE, 'r', encoding='utf-8') as f:
        try:
            adsh_db = json.load(f)
        except json.JSONDecodeError:
            pass

high_water_mark = ""
if os.path.exists(MARKER_FILE):
    with open(MARKER_FILE, 'r', encoding='utf-8') as f:
        high_water_mark = f.read().strip()

# 3. Setup RSS Feed Framework
fg = FeedGenerator()
fg.title(f'SEC Feed: {SEARCH_TERMS}')
fg.link(href='https://www.sec.gov')
fg.description('Automated AI summaries of primary SEC filings.')

# 4. Fetch All Interim Filings (Pagination loop with Strict Sorting)
all_hits = []
current_from = 0
page_size = 100
fetching = True
reached_marker = False 
force_marker_update = False 

while fetching:
    if current_from > 5000:
        print("Pagination limit reached. Stopping search and forcing a baseline reset.")
        force_marker_update = True 
        break
        
    url = f'https://efts.sec.gov/LATEST/search-index?q="{SEARCH_TERMS}"&forms={FORM_TYPES}&from={current_from}&size={page_size}&sort=desc'
    try:
        response = requests.get(url, headers=SEC_HEADERS, timeout=15)
        response.raise_for_status()
        data = response.json()
        hits = data.get('hits', {}).get('hits', [])

        if not hits:
            break

        for hit in hits:
            adsh = hit['_source'].get('adsh', '')
            if adsh == high_water_mark:
                fetching = False
                reached_marker = True 
                break
            all_hits.append(hit)
            
        if not high_water_mark:
            print("Initial cold start detected. Terminating pagination to establish baseline.")
            fetching = False

        current_from += len(hits) 
        time.sleep(0.15) 
    except Exception as e:
        print(f"Network error fetching SEC hits: {e}")
        break

new_entries = {}
new_high_water_mark = high_water_mark

if all_hits and (reached_marker or not high_water_mark or force_marker_update):
    new_high_water_mark = all_hits[0]['_source']['adsh']

if not high_water_mark and len(all_hits) > 20:
    all_hits = all_hits[:20]

# 5. Process the Pipeline
if all_hits:
    for hit in all_hits:
        source = hit['_source']
        cik = source.get('ciks', [''])[0]
        adsh = source.get('adsh', '')
        company_name = source.get('display_names', ['Unknown Company'])[0]
        file_date_str = source.get('file_date', '')
        file_time_str = source.get('file_datetime', 'Unknown Time')
        
        hit_id = hit.get('_id', '')
        exact_filename = hit_id.split(':')[-1] if ':' in hit_id else None

        if adsh not in adsh_db:
            adsh_no_dashes = adsh.replace('-', '')
            
            if not exact_filename or not exact_filename.endswith(('.htm', '.html')):
                continue
                
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_no_dashes}/{exact_filename}"
            try:
                # Increase sleep to 6 seconds to respect the ~10 RPM Free Tier Limit
                time.sleep(8) 
                doc_req = requests.get(doc_url, headers=SEC_HEADERS, timeout=10)
                soup = BeautifulSoup(doc_req.text, 'html.parser')
                clean_text = soup.get_text(separator=' ', strip=True)

                matches = list(re.finditer(re.escape(SEARCH_TERMS), clean_text, re.IGNORECASE))
                if not matches:
                    continue

                first_match_idx = matches[0].start()
                start_idx = max(0, first_match_idx - 1000)
                end_idx = min(len(clean_text), first_match_idx + 1500)
                context_chunk = clean_text[start_idx:end_idx]

                prompt = f"Summarize the purpose of this SEC filing related to '{SEARCH_TERMS}' for {company_name} based on the following contextual text excerpt: {context_chunk}. Keep it strictly to 2 sentences."
                
                try:
                    ai_response = model.generate_content(prompt)
                    try:
                        summary_text = ai_response.text
                    except ValueError:
                        summary_text = "AI summary unavailable (Content flagged by API safety filters). Please read the source document."
                except Exception as ai_err:
                    print(f"AI Generation failed for {adsh}: {ai_err}")
                    summary_text = "AI summary temporarily unavailable due to network or quota limits. Keyword matches are provided below."

                keyword_instances = []
                for match in matches[:10]:
                    start_char = max(0, match.start() - 300)
                    end_char = min(len(clean_text), match.end() + 300)
                    raw_snippet = clean_text[start_char:end_char]
                    
                    safe_snippet = html.escape(raw_snippet)
                    matched_word_safe = html.escape(match.group())
                    highlighted_snippet = safe_snippet.replace(matched_word_safe, f"<b>{matched_word_safe}</b>")
                    
                    keyword_instances.append(highlighted_snippet.strip())

                new_entries[adsh] = {
                    "company_name": company_name,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_no_dashes}/{adsh}-index.htm",
                    "summary": summary_text,
                    "file_date": file_date_str,
                    "file_time": file_time_str,
                    "keyword_instances": keyword_instances
                }
            except Exception as e:
                print(f"Error processing {adsh} at {doc_url}: {e}")
                continue

# 6. Merge dictionaries chronologically
combined_db = {**new_entries, **adsh_db}

# 7. Rebuild the XML feed enforcing the historical cap
final_db = dict(list(combined_db.items())[:2000])

for stored_adsh, entry_data in final_db.items():
    fe = fg.add_entry()
    fe.id(stored_adsh)
    fe.title(f"Filing: {html.escape(entry_data['company_name'])}")
    fe.link(href=entry_data['url'])
    
    try:
        pub_date = datetime.strptime(entry_data.get('file_date', ''), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        fe.pubDate(pub_date)
    except ValueError:
        pass
        
    instances_html = ""
    stored_instances = entry_data.get("keyword_instances", [])
    if stored_instances:
        instances_html += "<br><br><b>Keyword Excerpts:</b><ul>"
        for instance in stored_instances:
            instances_html += f"<li>... {instance} ...</li>"
        instances_html += "</ul>"
        
    formatted_output = f"""
    <b>Company Name:</b> {html.escape(entry_data['company_name'])}<br>
    <b>Filing Type:</b> {FORM_TYPES}<br>
    <b>Filing Date:</b> {entry_data.get('file_date', 'Unknown')}<br>
    <b>Filing Time:</b> {entry_data.get('file_time', 'Unknown')}<br><br>
    <b>AI Summary:</b><br>{entry_data['summary']}
    {instances_html}
    """
    
    fe.description(entry_data['summary'])
    fe.content(content=formatted_output, type='html')

# 8. Export the final Database and XML files
with open(DATABASE_FILE, 'w', encoding='utf-8') as f:
    json.dump(final_db, f, indent=4)

try:
    fg.rss_file('feed.xml')
    if all_hits and (reached_marker or not high_water_mark or force_marker_update):
        with open(MARKER_FILE, 'w', encoding='utf-8') as f:
            f.write(new_high_water_mark)
except Exception as e:
    print(f"Failed to write outputs: {e}")
