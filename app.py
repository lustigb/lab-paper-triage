import streamlit as st
import requests
import pandas as pd
import gspread
from datetime import datetime, timedelta
import time

# --- CONFIGURATION ---
LAB_MEMBERS = ["Select User...", "Albert", "Shinsuke", "Jaeson", "Brian"]
TOTAL_LAB_SIZE = len(LAB_MEMBERS) - 1 

MEMBER_COLORS = {
    "Albert": "#3498db",   # Blue
    "Shinsuke": "#2ecc71", # Green
    "Jaeson": "#e67e22",   # Orange
    "Brian": "#e74c3c"     # Red
}

# --- GOOGLE SHEETS CONNECTION ---
def get_db_connection():
    try:
        gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
        sh = gc.open_by_url(st.secrets["private_gsheets_url"])
        return sh
    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
        st.stop()

# --- CACHED DATA FETCHING ---
@st.cache_data(ttl=60)
def load_all_data_from_sheets():
    sh = get_db_connection()
    try:
        papers_data = sh.worksheet("papers").get_all_records()
        interest_data = sh.worksheet("interest").get_all_records()
        seen_data = sh.worksheet("seen").get_all_records()
        return papers_data, interest_data, seen_data
    except gspread.exceptions.WorksheetNotFound:
        return [], [], []
    except Exception as e:
        return [], [], []

def init_db():
    sh = get_db_connection()
    try: sh.worksheet("papers")
    except:
        ws = sh.add_worksheet(title="papers", rows=1000, cols=7)
        ws.append_row(["doi", "title", "authors", "abstract", "link", "category", "date"])
    try: sh.worksheet("interest")
    except:
        ws = sh.add_worksheet(title="interest", rows=1000, cols=3)
        ws.append_row(["doi", "user", "timestamp"])
    try: sh.worksheet("seen")
    except:
        ws = sh.add_worksheet(title="seen", rows=1000, cols=2)
        ws.append_row(["doi", "user"])

# --- BATCH UPDATE (VOTES AND TRASH) ---
def batch_update_all(user, selected_dois, trashed_dois, all_displayed_dois):
    sh = get_db_connection()
    ws_interest = sh.worksheet("interest")
    ws_seen = sh.worksheet("seen")
    
    # 1. HANDLE VOTES (Interest Sheet)
    data = ws_interest.get_all_records()
    df = pd.DataFrame(data)
    
    if df.empty:
        df = pd.DataFrame(columns=["doi", "user", "timestamp"])
    else:
        df['doi'] = df['doi'].astype(str)
        df['user'] = df['user'].astype(str)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changes_count = 0
    
    current_user_votes = df[df['user'] == user]['doi'].tolist()
    rows_to_add = []
    modified = False
    
    for doi in all_displayed_dois:
        is_checked = doi in selected_dois
        already_voted = doi in current_user_votes
        
        if is_checked and not already_voted:
            rows_to_add.append({"doi": doi, "user": user, "timestamp": timestamp})
            modified = True
            changes_count += 1
        elif not is_checked and already_voted:
            df = df[~((df['doi'] == doi) & (df['user'] == user))]
            modified = True
            changes_count += 1

    if modified:
        if rows_to_add:
            df = pd.concat([df, pd.DataFrame(rows_to_add)], ignore_index=True)
        ws_interest.clear()
        ws_interest.append_row(["doi", "user", "timestamp"])
        if not df.empty:
            ws_interest.append_rows(df.values.tolist())
            
    # 2. HANDLE TRASH (Seen Sheet)
    if trashed_dois:
        trash_rows = [[doi, user] for doi in trashed_dois]
        ws_seen.append_rows(trash_rows)
        changes_count += len(trash_rows)

    if changes_count > 0:
        st.cache_data.clear()
            
    return changes_count

# --- DATA PROCESSING ---
def get_shortlist_data(current_user):
    papers_data, interest_data, _ = load_all_data_from_sheets()
    
    df_papers = pd.DataFrame(papers_data)
    if df_papers.empty: return pd.DataFrame()
    df_papers['doi'] = df_papers['doi'].astype(str)
    df_papers = df_papers.drop_duplicates(subset=['doi'])

    df_interest = pd.DataFrame(interest_data)
    if df_interest.empty:
        df_papers['total_votes'] = 0
        df_papers['voter_names'] = ""
        df_papers['my_vote'] = False
        return pd.DataFrame()
    
    df_interest['doi'] = df_interest['doi'].astype(str)

    stats = df_interest.groupby('doi').agg(
        total_votes=('user', 'count'),
        voter_names=('user', lambda x: ','.join(x))
    ).reset_index()

    shortlist = pd.merge(df_papers, stats, on='doi', how='inner')
    shortlist = shortlist.sort_values(by=['total_votes', 'date'], ascending=[False, False])
    
    if 'shortlist_order' in st.session_state:
        frozen_order = st.session_state['shortlist_order']
        shortlist['doi_cat'] = pd.Categorical(shortlist['doi'], categories=frozen_order, ordered=True)
        shortlist = shortlist.sort_values('doi_cat')
    else:
        st.session_state['shortlist_order'] = shortlist['doi'].tolist()

    my_voted_dois = df_interest[df_interest['user'] == current_user]['doi'].tolist()
    shortlist['my_vote'] = shortlist['doi'].isin(my_voted_dois)
    
    return shortlist

def get_fresh_stream_by_date(current_user, start_date, end_date):
    papers_data, interest_data, seen_data = load_all_data_from_sheets()
    
    df_p = pd.DataFrame(papers_data)
    if df_p.empty: return pd.DataFrame()
    df_p['doi'] = df_p['doi'].astype(str)
    df_p = df_p.drop_duplicates(subset=['doi'])
    
    if interest_data:
        voted_dois = set(pd.DataFrame(interest_data)['doi'].astype(str))
    else:
        voted_dois = set()
        
    if seen_data:
        df_seen = pd.DataFrame(seen_data)
        df_seen['doi'] = df_seen['doi'].astype(str)
        my_seen_dois = set(df_seen[df_seen['user'] == current_user]['doi'])
    else:
        my_seen_dois = set()
        
    df_p['date_obj'] = pd.to_datetime(df_p['date']).dt.date
    mask_date = (df_p['date_obj'] >= start_date) & (df_p['date_obj'] <= end_date)
    mask_not_voted = ~df_p['doi'].isin(voted_dois)
    mask_not_seen = ~df_p['doi'].isin(my_seen_dois)
    
    fresh_df = df_p[mask_date & mask_not_voted & mask_not_seen].copy()
    fresh_df = fresh_df.sort_values(by='date', ascending=False)
    fresh_df['my_vote'] = False 
    fresh_df['total_votes'] = 0
    
    return fresh_df

def fetch_papers_range(start_date, end_date):
    s_date = start_date.strftime('%Y-%m-%d')
    e_date = end_date.strftime('%Y-%m-%d')
    
    sh = get_db_connection()
    ws = sh.worksheet("papers")
    
    existing_data = ws.get_all_records()
    if existing_data:
        seen_dois = set(pd.DataFrame(existing_data)['doi'].astype(str))
    else:
        seen_dois = set()

    cursor = 0
    new_rows = []
    
    progress_text = f"Fetching papers from {s_date} to {e_date}..."
    my_bar = st.sidebar.progress(0, text=progress_text)

    for i in range(5): 
        my_bar.progress((i+1) * 20, text=f"Scanning page {i+1}...")
        url = f"https://api.biorxiv.org/details/biorxiv/{s_date}/{e_date}/{cursor}?category=neuroscience"
        try:
            response = requests.get(url).json()
            if response.get('messages', [{}])[0].get('status') == 'no posts found': break
            papers = response.get('collection', [])
            if not papers: break
            
            for p in papers:
                if p.get('category').lower() == 'neuroscience':
                    if p['doi'] not in seen_dois:
                        seen_dois.add(p['doi'])
                        link = f"https://www.biorxiv.org/content/{p['doi']}v1"
                        row = [p['doi'], p['title'], p['authors'], p['abstract'], link, p['category'], p['date']]
                        new_rows.append(row)
                        
            cursor += len(papers)
            if len(papers) < 100: break
            time.sleep(0.5) 
        except Exception: break
        
    my_bar.empty()
    
    if new_rows:
        ws.append_rows(new_rows)
        st.cache_data.clear()
        return len(new_rows)
    return 0

# --- MAIN APP UI ---
def main():
    st.set_page_config(page_title="LabRxiv", layout="wide") 
    
    st.markdown("""
        <style>
               .block-container { padding-top: 2rem; padding-bottom: 5rem; }
               .badge {
                   display: inline-block; padding: 2px 8px; margin-right: 4px; margin-top: 4px;
                   border-radius: 12px; color: white; font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
               }
               div[data-testid="stVerticalBlockBorderWrapper"] {
                   border: 1px solid #e0e0e0; transition: all 0.2s ease-in-out;
               }
               div[data-testid="stVerticalBlockBorderWrapper"]:hover {
                   border: 2px solid #ff4b4b !important; 
                   background-color: #f9f9f9;
                   box-shadow: 0 4px 10px rgba(0,0,0,0.1);
                   transform: translateX(2px); 
               }
               .share-text { font-size: 1.1rem; font-weight: 800; color: #444; line-height: 1.2; margin-bottom: 4px; }
               div[data-testid="stButton"] { text-align: center; }
        </style>
        """, unsafe_allow_html=True)

    if 'db_init' not in st.session_state:
        init_db()
        st.session_state['db_init'] = True
    
    st.sidebar.title("🧠 LabRxiv")
    user_name = st.sidebar.selectbox("Current User:", LAB_MEMBERS)
    st.sidebar.divider()
    
    st.sidebar.markdown("**View Settings**")
    # NEW TOGGLE FOR ABSTRACT VIEW
    expand_all = st.sidebar.toggle("👁️ Abstract View", value=False)
    
    st.sidebar.divider()
    
    st.sidebar.markdown("**Select Triage Date Range**")
    today = datetime.today()
    last_week = today - timedelta(days=7)
    date_range = st.sidebar.date_input("Range", (last_week, today), format="YYYY-MM-DD")
    
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_d, end_d = date_range
        if st.sidebar.button(f"⬇️ Load Papers ({start_d} to {end_d})"):
            added = fetch_papers_range(start_d, end_d)
            if added > 0: st.toast(f"Downloaded {added} new papers!")
            else: st.toast("Papers already in database.")
            st.rerun()
    else:
        start_d, end_d = today, today

    if user_name == "Select User...":
        st.warning("Please select your name in the sidebar.")
        return

    all_visible_dois = []
    selected_dois = []
    trashed_dois = []

    triaged_df = get_shortlist_data(user_name)
    total_system_votes = triaged_df['total_votes'].sum() if not triaged_df.empty else 1

    st.markdown("### 🏆 Lab Shortlist (Active)")
    if triaged_df.empty: st.info("No papers shortlisted yet.")
    
    for index, row in triaged_df.iterrows():
        doi = row['doi']
        all_visible_dois.append(doi)
        
        db_voted = row['my_vote']
        toggle_key = f"vote_state_{doi}_{user_name}"
        if toggle_key not in st.session_state: st.session_state[toggle_key] = False
        user_clicked_toggle = st.session_state[toggle_key]
        
        is_effectively_selected = (db_voted != user_clicked_toggle)
        if is_effectively_selected: selected_dois.append(doi)
        
        if db_voted and not user_clicked_toggle: btn_label = "🗑️"
        elif db_voted and user_clicked_toggle: btn_label = "❌ Remove"
        elif not db_voted and user_clicked_toggle: btn_label = "✅ Voted"
        else: btn_label = "👍"

        with st.container(border=True):
            c_vote, c_btn, c_content = st.columns([0.12, 0.12, 0.76])
            
            with c_vote:
                share_pct = row['total_votes'] / total_system_votes
                st.markdown(f'<div class="share-text">{share_pct:.0%} Share</div>', unsafe_allow_html=True)
                st.progress(share_pct)

            with c_btn:
                voters = str(row['voter_names']).split(',') if row['voter_names'] else []
                if voters:
                    with st.expander(f"👥 {len(voters)}"):
                        html_badges = ""
                        for v in voters:
                            color = MEMBER_COLORS.get(v, "#7f8c8d")
                            html_badges += f'<span class="badge" style="background-color:{color};">{v}</span>'
                        st.markdown(html_badges, unsafe_allow_html=True)
                
                if st.button(btn_label, type="secondary", key=f"btn_{doi}_{user_name}"):
                    st.session_state[toggle_key] = not st.session_state[toggle_key]
                    st.rerun()

            with c_content:
                # CONTROLLED EXPANSION VIA 'expanded' PARAMETER
                with st.expander(f"**{row['title']}**", expanded=expand_all):
                    st.caption(f"{row['authors']} ({row['date']})")
                    st.write(row['abstract'])
                    st.markdown(f"[Link]({row['link']})")

    st.divider()

    fresh_df = get_fresh_stream_by_date(user_name, start_d, end_d)
    c_fresh_h, c_fresh_cnt = st.columns([0.8, 0.2])
    c_fresh_h.markdown(f"### 🌊 Fresh Stream ({start_d} to {end_d})")
    if not fresh_df.empty: c_fresh_cnt.caption(f"Showing {len(fresh_df)} papers")
    
    if fresh_df.empty: st.info(f"No papers found for this range.")
    
    for index, row in fresh_df.iterrows():
        doi = row['doi']
        all_visible_dois.append(doi)
        
        vote_key = f"vote_state_{doi}_{user_name}"
        if vote_key not in st.session_state: st.session_state[vote_key] = False
        user_clicked_vote = st.session_state[vote_key]
        
        if user_clicked_vote:
            selected_dois.append(doi)
            vote_label = "✅ Voted"
        else:
            vote_label = "👍"

        trash_key = f"trash_state_{doi}_{user_name}"
        if trash_key not in st.session_state: st.session_state[trash_key] = False
        user_clicked_trash = st.session_state[trash_key]

        if user_clicked_trash:
            trashed_dois.append(doi)
            trash_label = "❌ Remove"
        else:
            trash_label = "🗑️"

        with st.container(border=True):
            c_vote_btn, c_trash_btn, c_content = st.columns([0.10, 0.10, 0.80])
            
            with c_vote_btn:
                if st.button(vote_label, type="secondary", key=f"f_v_btn_{doi}_{user_name}"):
                    st.session_state[vote_key] = not st.session_state[vote_key]
                    st.rerun()
            
            with c_trash_btn:
                if st.button(trash_label, type="secondary", key=f"f_t_btn_{doi}_{user_name}"):
                    st.session_state[trash_key] = not st.session_state[trash_key]
                    st.rerun()

            with c_content:
                # CONTROLLED EXPANSION
                with st.expander(f"{row['title']}", expanded=expand_all):
                    st.caption(f"{row['authors']} ({row['date']})")
                    st.write(row['abstract'])
                    st.markdown(f"[Link]({row['link']})")

    st.sidebar.divider()
    if st.sidebar.button("💾 Submit Votes", type="primary"):
        changes = batch_update_all(user_name, set(selected_dois), set(trashed_dois), all_visible_dois)
        
        keys_to_reset = [k for k in st.session_state.keys() if (f"vote_state_" in k or f"trash_state_" in k) and user_name in k]
        for k in keys_to_reset:
            st.session_state[k] = False
            
        if changes > 0:
            st.toast(f"Processed {changes} updates!")
            if 'shortlist_order' in st.session_state:
                del st.session_state['shortlist_order']
            st.rerun()
        else:
            st.toast("No changes detected.")

if __name__ == "__main__":
    main()
