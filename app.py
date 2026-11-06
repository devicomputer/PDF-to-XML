import streamlit as st
import re
import PyPDF2
import uuid
import xml.sax.saxutils as saxutils
import pdfplumber
import os
import pandas as pd
import json
from datetime import datetime
import io
from datetime import datetime

# ==========================================
# ENGINE 1: IDBI BANK PARSER
# ==========================================
def parse_idbi_bank(raw_text):
    entries = []
    lines = raw_text.split('\n')
    
    pattern = re.compile(
        r'^\d+\s+'                                                
        r'(?:\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\s+[AP]M|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+' 
        r'(\d{2}/\d{2}/\d{4})\s+'                                 
        r'(.*?)\s+'                                               
        r'(Dr\.|Cr\.)\s+INR\s+'                                   
        r'([\d,]+\.\d{2})\s+'                                     
        r'([\d,]+\.\d{2})',                                       
        re.IGNORECASE
    )
    
    for line in lines:
        line = line.strip()
        match = pattern.search(line)
        
        if match:
            if match.group(3).strip().title() == "Dr.":
                debit, credit = match.group(4), ""
            else:
                debit, credit = "", match.group(4)
                
            entries.append({
                "Date": match.group(1),
                "Particulars / Narration": match.group(2).strip(),
                "Withdrawal (Dr)": debit,
                "Deposit (Cr)": credit,
                "Balance": match.group(5)
            })
            
    # 🌟 IDBI Bank Data Reversal Patch for Tally XML
    return entries[::-1]

# ==========================================
# ENGINE 2: BANK OF BARODA PARSER
# ==========================================
def parse_bob_bank(raw_text):
    entries = []
    blocks = []
    current_block = ""
    lines = raw_text.replace('\r', '').split('\n')

    for line in lines:
        line = line.strip()
        if re.match(r'^\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}', line):
            if current_block: blocks.append(current_block)
            current_block = line
        elif current_block:
            current_block += " " + line
    if current_block: blocks.append(current_block)

    for block in blocks:
        date_match = re.search(r'^(\d{2}/\d{2}/\d{4})', block)
        if not date_match: continue
        
        bal_match = re.search(r'([\d,]+\.\d{2})(Cr|Dr)\b', block, re.IGNORECASE)
        if not bal_match: continue
        
        full_bal_str = bal_match.group(0)
        block_no_bal = block.replace(full_bal_str, '')
        amounts = re.findall(r'\b\d{1,3}(?:,\d{3})*\.\d{2}\b', block_no_bal)
        
        if not amounts: continue
        txn_amount_str = amounts[-1]
        
        idx_txn = block.rfind(txn_amount_str)
        idx_bal = block.rfind(full_bal_str)

        if idx_txn < idx_bal:
            debit, credit = "", txn_amount_str
        else:
            debit, credit = txn_amount_str, ""

        narration = re.sub(r'^\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}', '', block_no_bal)
        narration = narration.replace(txn_amount_str, '').strip()
        narration = re.sub(r'\s+', ' ', narration)

        entries.append({
            "Date": date_match.group(1),
            "Particulars / Narration": narration,
            "Withdrawal (Dr)": debit,
            "Deposit (Cr)": credit,
            "Balance": bal_match.group(1) + bal_match.group(2).title()
        })
    return entries

# ==========================================
# ENGINE 3: HDFC BANK PARSER 
# ==========================================
def parse_hdfc_bank(raw_text):
    entries = []
    lines = raw_text.split('\n')
    
    first_line_pattern = re.compile(
        r'^(\d{2}/\d{2}/\d{2})\s+'           
        r'(.*?)\s+'                           
        r'(\d{2}/\d{2}/\d{2})\s+'             
        r'([\d,]+\.\d{2})\s+'                 
        r'([\d,]+\.\d{2})$'                   
    )
    
    junk_keywords = [
        'PAGENO', 'ACCOUNTBRANCH', 'ADDRESS', 'CITY', 'STATE', 'PHONENO', 
        'ODLIMIT', 'CURRENCY', 'EMAIL', 'CUSTID', 'ACCOUNTNO', 'A/COPENDATE', 
        'ACCOUNTSTATUS', 'ACCOUNTTYPE', 'BRANCHCODE', 'NOMINATION', 
        'STATEMENTOF ACCOUNT', 'DATE NARRATION', '*CLOSINGBALANCE', 
        'CONTENTSOFTHIS', 'HDFCBANKGSTIN', 'REGISTEREDOFFICE', 'JOINTHOLDERS', 
        'RTGS/NEFT'
    ]
    
    raw_txns = []
    current_txn = None
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        match = first_line_pattern.search(line)
        if match:
            if current_txn: raw_txns.append(current_txn)
            
            current_txn = {
                "Date": match.group(1),
                "Narration": match.group(2),
                "Amount": float(match.group(4).replace(',', '')),
                "Balance": float(match.group(5).replace(',', '')),
                "AmountStr": match.group(4),
                "BalanceStr": match.group(5)
            }
        elif current_txn:
            is_junk = any(kw in line.upper() for kw in junk_keywords)
            if not is_junk:
                current_txn["Narration"] += " " + line
                
    if current_txn: raw_txns.append(current_txn)
        
    for i in range(len(raw_txns)):
        txn = raw_txns[i]
        amt = txn["Amount"]
        bal = txn["Balance"]
        dr_cr = "Dr" 
        
        if i > 0:
            prev_bal = raw_txns[i-1]["Balance"]
            if abs(prev_bal + amt - bal) < 0.01:
                dr_cr = "Cr"
            elif abs(prev_bal - amt - bal) < 0.01:
                dr_cr = "Dr"
        else:
            if any(kw in txn["Narration"].upper() for kw in ["RECEIVED", "CREDIT", "NEFT-CR", "RTGS-CR"]):
                dr_cr = "Cr"

        date_parts = txn["Date"].split('/')
        std_date = f"{date_parts[0]}/{date_parts[1]}/20{date_parts[2]}" if len(date_parts) == 3 else txn["Date"]
            
        entries.append({
            "Date": std_date,
            "Particulars / Narration": txn["Narration"],
            "Withdrawal (Dr)": txn["AmountStr"] if dr_cr == "Dr" else "",
            "Deposit (Cr)": txn["AmountStr"] if dr_cr == "Cr" else "",
            "Balance": txn["BalanceStr"]
        })
        
    return entries

# ==========================================
# ENGINE 4: EQUITAS BANK PARSER
# ==========================================
def parse_equitas_bank(raw_text):
    entries = []
    lines = raw_text.split('\n')
    
    first_line_pattern = re.compile(
        r'^(\d{2}-[A-Za-z]{3}-\d{4})\s+' 
        r'(\S+)\s+'                      
        r'(.*?)\s+'                      
        r'([\d,]+\.\d{2})\s+'            
        r'([\d,]+\.\d{2})$'              
    )
    
    junk_keywords = [
        'EQUITAS SMALL', '4TH FLOOR', 'SPENCER PLAZA', 'TOLL FREE', 'CIN NO',
        'STATEMENT FOR THE PERIOD', 'DATE REFERENCE NO', 'INR INR INR', 'PAGE ', 'CLOSINGBALANCE'
    ]
    
    raw_txns = []
    current_txn = None
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        match = first_line_pattern.search(line)
        if match:
            if current_txn: raw_txns.append(current_txn)
            
            current_txn = {
                "Date": match.group(1),
                "RefNo": match.group(2),
                "Narration": match.group(3),
                "Amount": float(match.group(4).replace(',', '')),
                "Balance": float(match.group(5).replace(',', '')),
                "AmountStr": match.group(4),
                "BalanceStr": match.group(5)
            }
        elif current_txn:
            is_junk = any(kw in line.upper() for kw in junk_keywords)
            if not is_junk:
                current_txn["Narration"] += " " + line
                
    if current_txn: raw_txns.append(current_txn)
        
    for i in range(len(raw_txns)):
        txn = raw_txns[i]
        amt = txn["Amount"]
        bal = txn["Balance"]
        dr_cr = "Dr" 
        
        if i > 0:
            prev_bal = raw_txns[i-1]["Balance"]
            if abs(prev_bal + amt - bal) < 0.01:
                dr_cr = "Cr"
            elif abs(prev_bal - amt - bal) < 0.01:
                dr_cr = "Dr"
        else:
            if any(kw in txn["Narration"].upper() for kw in ["DEP", "CREDIT", "CR", "NEFT CR"]):
                dr_cr = "Cr"
            elif abs(bal - amt) < 0.01: 
                dr_cr = "Cr"

        try:
            date_obj = datetime.strptime(txn["Date"], "%d-%b-%Y")
            std_date = date_obj.strftime("%d/%m/%Y")
        except:
            std_date = txn["Date"]
            
        entries.append({
            "Date": std_date,
            "Particulars / Narration": txn["RefNo"] + " " + txn["Narration"],
            "Withdrawal (Dr)": txn["AmountStr"] if dr_cr == "Dr" else "",
            "Deposit (Cr)": txn["AmountStr"] if dr_cr == "Cr" else "",
            "Balance": txn["BalanceStr"]
        })
        
    return entries

# ==========================================
# ENGINE 5: AU SMALL FINANCE BANK PARSER
# ==========================================
def parse_au_bank(raw_text):
    entries = []
    clean_text = raw_text.replace('\r', '')
    
    amt_matches = list(re.finditer(r'\b([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\b', clean_text))
    date_matches = list(re.finditer(r'(\d{2}\s[A-Za-z]{3}\s\d{4})\s\d{2}\s[A-Za-z]{3}\s\d{4}', clean_text))
    
    junk_keywords = [
        'Transaction Date', 'Value Date', 'Description/Narration', 'Cheque/', 'Reference No.', 
        'Debit', 'Credit', 'Balance', 'Opening Balance', 'Closing Balance', 'Statement Date', 
        'AU Royale', 'Customer ID', 'Account Number', '₹'
    ]
    
    for i in range(len(amt_matches)):
        amt_str = amt_matches[i].group(1)
        bal_str = amt_matches[i].group(2)
        
        if i < len(date_matches):
            date_str = date_matches[i].group(1)
        else:
            date_str = "01 Jan 2026"
            
        start_idx = amt_matches[i-1].end() if i > 0 else 0
        end_idx = amt_matches[i].start()
        chunk = clean_text[start_idx:end_idx]
        
        chunk = re.sub(r'\d{2}\s[A-Za-z]{3}\s\d{4}\s\d{2}\s[A-Za-z]{3}\s\d{4}', ' ', chunk)
        narration = chunk.replace('\n', ' ').strip()
        for kw in junk_keywords:
            narration = re.sub(kw, '', narration, flags=re.IGNORECASE)
        narration = re.sub(r'\s+', ' ', narration).strip()
        
        amt = float(amt_str.replace(',', ''))
        bal = float(bal_str.replace(',', ''))
        
        dr_cr = "Dr"
        if i > 0:
            prev_bal = float(amt_matches[i-1].group(2).replace(',', ''))
            if abs(prev_bal + amt - bal) < 0.01:
                dr_cr = "Cr"
            elif abs(prev_bal - amt - bal) < 0.01:
                dr_cr = "Dr"
        else:
            if "CR" in narration.upper() or "CREDIT" in narration.upper():
                dr_cr = "Cr"
                
        try:
            std_date = datetime.strptime(date_str, "%d %b %Y").strftime("%d/%m/%Y")
        except:
            std_date = date_str
            
        entries.append({
            "Date": std_date,
            "Particulars / Narration": narration,
            "Withdrawal (Dr)": amt_str if dr_cr == "Dr" else "",
            "Deposit (Cr)": amt_str if dr_cr == "Cr" else "",
            "Balance": bal_str
        })
        
    return entries

# ==========================================
# ENGINE 6: KOTAK MAHINDRA BANK PARSER
# ==========================================
def parse_kotak_bank(raw_text):
    entries = []
    lines = raw_text.split('\n')
    
    blocks = []
    current_block = ""

    for line in lines:
        line = line.strip()
        if re.match(r'^\d+\s+\d{2}\s+[A-Za-z]{3}\s+\d{4}', line):
            if current_block: blocks.append(current_block)
            current_block = line
        elif current_block and line and not line.startswith("Statement generated") and not line.startswith("HEAVY MOTORS") and not line.startswith("Account Statement") and not line.startswith("# TRANSACTION"):
            current_block += " " + line
            
    if current_block: blocks.append(current_block)

    for block in blocks:
        date_match = re.search(r'^\d+\s+(\d{2}\s+[A-Za-z]{3}\s+\d{4})', block)
        if not date_match: continue
        date_str = date_match.group(1)

        amt_bal_match = re.search(r'([+-][\d,]+\.\d{2})\s+([\d,]+\.\d{2})', block)
        if not amt_bal_match: continue
        
        full_amt_str = amt_bal_match.group(1)
        balance_str = amt_bal_match.group(2)
        
        amt_value_str = full_amt_str.replace('+', '').replace('-', '')

        if full_amt_str.startswith('-'):
            debit = amt_value_str
            credit = ""
        elif full_amt_str.startswith('+'):
            debit = ""
            credit = amt_value_str
        else:
             debit = ""
             credit = ""
             
        narration = block.replace(amt_bal_match.group(0), "").strip()
        narration = re.sub(r'^\d+\s+\d{2}\s+[A-Za-z]{3}\s+\d{4}', '', narration).strip()
        narration = re.sub(r'\s+', ' ', narration)

        try:
            std_date = datetime.strptime(date_str, "%d %b %Y").strftime("%d/%m/%Y")
        except:
            std_date = date_str

        entries.append({
            "Date": std_date,
            "Particulars / Narration": narration,
            "Withdrawal (Dr)": debit,
            "Deposit (Cr)": credit,
            "Balance": balance_str
        })

    return entries

# ==========================================
# ENGINE 7: AXIS BANK PARSER
# ==========================================
def parse_axis_bank(raw_text):
    entries = []
    lines = raw_text.split('\n')
    
    temp_narration = ""
    opening_bal = 0.0
    ob_match = re.search(r'OPENING BALANCE\s+([\d,]+\.\d{2})', raw_text)
    if ob_match:
        opening_bal = float(ob_match.group(1).replace(',', ''))
        
    junk_keywords = [
        "Statement of Axis", "Tran Date", "OPENING BALANCE", "Customer ID", 
        "IFSC", "MICR", "Nominee", "Registered", "PAN :", "Scheme :", "Joint Holder"
    ]
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        if any(kw in line for kw in junk_keywords):
            continue
            
        date_match = re.match(r'^(\d{2}-\d{2}-\d{4})', line)
        
        if date_match:
            date_str = date_match.group(1)
            std_date = date_str.replace('-', '/')
            
            amt_bal_match = re.search(r'([\d,]+\.\d{2})\s+([\d,]+\.\d{2})(?:\s+\w+)?$', line)
            
            if amt_bal_match:
                amt_str = amt_bal_match.group(1)
                bal_str = amt_bal_match.group(2)
                
                amt = float(amt_str.replace(',', ''))
                bal = float(bal_str.replace(',', ''))
                
                line_narration = line[len(date_str):amt_bal_match.start()].strip()
                full_narration = f"{temp_narration} {line_narration}".strip()
                temp_narration = "" 
                
                prev_bal = float(entries[-1]["Balance"].replace(',', '')) if entries else opening_bal
                
                dr_cr = "Dr"
                if abs(prev_bal + amt - bal) < 0.01:
                    dr_cr = "Cr"
                elif abs(prev_bal - amt - bal) < 0.01:
                    dr_cr = "Dr"
                
                entries.append({
                    "Date": std_date,
                    "Particulars / Narration": full_narration,
                    "Withdrawal (Dr)": amt_str if dr_cr == "Dr" else "",
                    "Deposit (Cr)": amt_str if dr_cr == "Cr" else "",
                    "Balance": bal_str
                })
        else:
            temp_narration = f"{temp_narration} {line}".strip()
            
    return entries

# ==========================================
# ENGINE 8: STATE BANK OF INDIA (SBI) PARSER
# ==========================================
def parse_sbi_bank(raw_text):
    entries = []
    lines = raw_text.split('\n')
    
    txn_pattern = re.compile(
        r'^(\d{2}/\d{2}/\d{4})\s+(?:\d{2}/\d{2}/\d{4})\s+(.*?)\s+(\S+)\s+([\d,]+\.\d{2}|-)\s+([\d,]+\.\d{2}|-)\s+([\d,]+\.\d{2})$'
    )
    
    current_txn = None
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        match = txn_pattern.match(line)
        if match:
            if current_txn:
                entries.append(current_txn)
                
            date = match.group(1)
            narration = match.group(2)
            chq_no = match.group(3) if match.group(3) != '-' else ''
            debit = match.group(4) if match.group(4) != '-' else ''
            credit = match.group(5) if match.group(5) != '-' else ''
            balance = match.group(6)
            
            full_narr = f"{narration} {chq_no}".strip()
            
            current_txn = {
                "Date": date,
                "Particulars / Narration": full_narr,
                "Withdrawal (Dr)": debit,
                "Deposit (Cr)": credit,
                "Balance": balance
            }
        elif current_txn:
            upper_line = line.upper()
            junk_starts = ["PAGE NO", "STATEMENT OF", "STATE BANK OF INDIA"]
            junk_exact = ["BALANCE", "DEP TFR", "WDL TFR", "DESCRIPTION", "REF NO./CHEQUE NO."]
            
            is_junk = any(upper_line.startswith(j) for j in junk_starts) or upper_line in junk_exact
            
            if not is_junk:
                current_txn["Particulars / Narration"] += " " + line
                
    if current_txn:
        entries.append(current_txn)
        
    return entries

# ==========================================
# ENGINE 9: PUNJAB NATIONAL BANK (PNB) PARSER
# ==========================================
def parse_pnb_bank(raw_text):
    entries = []
    lines = raw_text.split('\n')
    
    pattern = re.compile(
        r'^(\d{2}/\d{2}/\d{4})\s+(?:(\S+)\s+)?([\d,]+\.\d+)\s+(CR|DR)\s+([\d,]+\.\d+)\s+(.*)$', 
        re.IGNORECASE
    )
    
    current_txn = None
    
    junk_keywords = [
        "STATEMENT OF ACCOUNT", "DATE INSTRUMENT ID", "CUSTOMER NAME", 
        "BRANCH DETAILS", "IFSC:", "MICR CODE:", "CUSTOMER DETAILS"
    ]
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        match = pattern.match(line)
        if match:
            if current_txn:
                entries.append(current_txn)
                
            date = match.group(1)
            inst_id = match.group(2) if match.group(2) else ""
            amount = match.group(3)
            txn_type = match.group(4).upper()
            balance = match.group(5)
            narration = match.group(6)
            
            full_narr = f"{inst_id} {narration}".strip()
            
            current_txn = {
                "Date": date,
                "Particulars / Narration": full_narr,
                "Withdrawal (Dr)": amount if txn_type == "DR" else "",
                "Deposit (Cr)": amount if txn_type == "CR" else "",
                "Balance": balance
            }
        elif current_txn:
            is_junk = any(kw in line.upper() for kw in junk_keywords)
            if re.match(r'^Date:\s*\d{2}/\d{2}/\d{4}.*Page', line, re.IGNORECASE):
                is_junk = True
                
            if not is_junk:
                current_txn["Particulars / Narration"] += " " + line
                
    if current_txn:
        entries.append(current_txn)
        
    return entries

# ==========================================
# MASTER ROUTER: SELECTS THE CORRECT ENGINE
# ==========================================
def master_parser(selected_bank, raw_text):
    if selected_bank == "IDBI Bank":
        return parse_idbi_bank(raw_text)
    elif selected_bank == "Bank of Baroda":
        return parse_bob_bank(raw_text)
    elif selected_bank == "HDFC Bank":
        return parse_hdfc_bank(raw_text)
    elif selected_bank == "Equitas Bank":     
        return parse_equitas_bank(raw_text)   
    elif selected_bank == "AU Small Finance Bank":  
        return parse_au_bank(raw_text)
    elif selected_bank == "Kotak Mahindra Bank":
        return parse_kotak_bank(raw_text)
    elif selected_bank == "Axis Bank":           
        return parse_axis_bank(raw_text)   
    elif selected_bank == "State Bank of India":
        return parse_sbi_bank(raw_text)   
    elif selected_bank == "Punjab National Bank":
        return parse_pnb_bank(raw_text)   
    else:
        return []

# ==========================================
# EXCEL / CSV MASTER PARSER (SMART ENGINE)
# ==========================================
def master_excel_parser(selected_bank, df_raw):
    entries = []
    
    header_idx = -1
    for idx, row in df_raw.iterrows():
        row_str = " ".join([str(val).lower() for val in row.values])
        if "date" in row_str and ("balance" in row_str or "amount" in row_str):
            header_idx = idx
            break
            
    if header_idx != -1:
        df = df_raw.iloc[header_idx+1:].copy()
        df.columns = df_raw.iloc[header_idx]
        df.columns = [str(c).strip().lower() for c in df.columns]
        
        date_col = next((c for c in df.columns if 'date' in c), None)
        narration_col = next((c for c in df.columns if 'narration' in c or 'particular' in c or 'description' in c or 'remark' in c), None)
        bal_col = next((c for c in df.columns if 'balance' in c), None)
        
        dr_col = next((c for c in df.columns if 'withdrawal' in c or 'debit' in c or 'dr' in c), None)
        cr_col = next((c for c in df.columns if 'deposit' in c or 'credit' in c or 'cr' in c), None)
        
        for _, row in df.iterrows():
            if pd.isna(row.get(date_col)): continue 
            
            date_val = str(row.get(date_col, "")).split()[0] if not pd.isna(row.get(date_col)) else ""
            narr_val = str(row.get(narration_col, "")) if not pd.isna(row.get(narration_col)) else ""
            bal_val = str(row.get(bal_col, "")) if not pd.isna(row.get(bal_col)) else ""
            
            dr_val = str(row.get(dr_col, "")) if dr_col and not pd.isna(row.get(dr_col)) else ""
            cr_val = str(row.get(cr_col, "")) if cr_col and not pd.isna(row.get(cr_col)) else ""
            
            if dr_val.lower() == "nan": dr_val = ""
            if cr_val.lower() == "nan": cr_val = ""
            if narr_val.lower() == "nan": narr_val = ""
            
            entries.append({
                "Date": date_val,
                "Particulars / Narration": narr_val,
                "Withdrawal (Dr)": dr_val,
                "Deposit (Cr)": cr_val,
                "Balance": bal_val
            })
            
    return entries

# ==========================================
# BULK XML GENERATOR (SMART MAPPING & CONTRA ENABLED)
# ==========================================
def generate_tally_xml(entries, selected_bank, rules):
    xml_vouchers = ""
    for entry in entries:
        debit = entry.get("Withdrawal (Dr)", "").replace(",", "")
        credit = entry.get("Deposit (Cr)", "").replace(",", "")
        if not debit and not credit: continue
            
        amt = debit if debit else credit
        is_deemed_positive = "Yes" if debit else "No"
        narration = saxutils.escape(entry.get("Particulars / Narration", ""))
        
        # 🌟 1. SMART LEDGER MAPPING LOGIC
        target_ledger = "Suspense A/c" # Default
        for keyword, mapped_ledger in rules.items():
            if keyword.upper() in narration.upper():
                target_ledger = saxutils.escape(mapped_ledger)
                break 
                
        # 🌟 2. CONTRA VOUCHER LOGIC (The Tally Rule)
        # अगर लेजर का नाम Cash है, तो Payment/Receipt की जगह Contra वाउचर बनेगा
        if target_ledger.upper() in ["CASH", "CASH A/C", "CASH IN HAND"]:
            vch_type = "Contra"
        else:
            vch_type = "Payment" if debit else "Receipt"
        
        try:
            tally_date = datetime.strptime(entry.get("Date", ""), "%d/%m/%Y").strftime("%Y%m%d")
        except Exception:
            tally_date = "20260101"
        
        xml_vouchers += f"""
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER VCHTYPE="{vch_type}" ACTION="Create">
            <DATE>{tally_date}</DATE>
            <VOUCHERTYPENAME>{vch_type}</VOUCHERTYPENAME>
            <NARRATION>{narration}</NARRATION>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{target_ledger}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>{is_deemed_positive}</ISDEEMEDPOSITIVE>
              <AMOUNT>{amt}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{saxutils.escape(selected_bank)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>{"No" if is_deemed_positive == "Yes" else "Yes"}</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amt}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
          </VOUCHER>
        </TALLYMESSAGE>"""

    return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>{xml_vouchers}</REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""

# ==========================================
# STREAMLIT UI SETUP
# ==========================================
st.set_page_config(page_title="PDF to Tally XML", layout="wide")

st.markdown("""
    <style>
    div[data-testid="InputInstructions"] { display: none !important; }
    div[data-baseweb="select"] > div { border-color: #E2E8F0 !important; }
    div[data-baseweb="select"] > div:focus-within {
        border-color: #3B82F6 !important; 
        box-shadow: 0 0 0 1px #3B82F6 !important;
    }
    @media (max-width: 768px) {
        .block-container, [data-testid="stAppViewBlockContainer"] {
            padding-bottom: 60vh !important; 
        }
        ul[data-baseweb="menu"], div[data-baseweb="popover"] ul {
            max-height: 160px !important;
        }
    }
    </style>
""", unsafe_allow_html=True)

st.title("Multi-Bank Statement to Tally XML 🧾")

if 'raw_text' not in st.session_state: st.session_state.raw_text = None
if 'parsed_data' not in st.session_state: st.session_state.parsed_data = None

st.markdown("---")

# ==========================================
# STEP 1: SELECT BANK & UPLOAD
# ==========================================
st.subheader("Step 1: Select Bank & Upload Statement")

bank_options = [
    "AU Small Finance Bank", "Axis Bank", "Bank of Baroda", "Equitas Bank", 
    "HDFC Bank", "IDBI Bank", "Kotak Mahindra Bank", "Punjab National Bank", "State Bank of India"
]

selected_bank = st.selectbox(
    "Select the Bank Statement Format:", 
    options=sorted(bank_options),
    index=None, 
    placeholder="Enter or Select your Bank..."
)

if selected_bank:
    uploaded_file = st.file_uploader(f"Drop your {selected_bank} Statement here (PDF / EXCEL / CSV)", type=["pdf", "csv", "xlsx", "xls"])

    if uploaded_file is not None:
        file_ext = uploaded_file.name.split('.')[-1].lower()
        is_pdf = (file_ext == "pdf")
        
        is_encrypted = False
        if is_pdf:
            try:
                pdf_reader = PyPDF2.PdfReader(uploaded_file)
                if pdf_reader.is_encrypted:
                    if pdf_reader.decrypt("") == 0:
                        is_encrypted = True
            except Exception:
                pass 
        
        with st.form("xml_extraction_form"):
            pdf_password = ""
            if is_encrypted:
                st.warning("🔒 This PDF is password-protected!")
                pdf_password = st.text_input("Enter PDF Password:", type="password", placeholder="Enter password to unlock...")
            elif not is_pdf:
                st.info(f"📊 {file_ext.upper()} Format Detected! Direct table extraction ready.")
            
            extract_clicked = st.form_submit_button("🔍 Extract Transactions")
        
        if extract_clicked:
            if is_encrypted and not pdf_password:
                st.error("❌ You must enter the correct password to proceed!")
            else:
                try:
                    all_parsed_entries = []
                    status_text = st.empty()
                    
                    if is_pdf:
                        unique_id = uuid.uuid4().hex
                        temp_pdf_path = f"temp_{unique_id}_{uploaded_file.name}"
                        
                        with open(temp_pdf_path, "wb") as f:
                            f.write(uploaded_file.getbuffer())
                        
                        full_raw_text = ""
                        with pdfplumber.open(temp_pdf_path, password=pdf_password if is_encrypted else "") as pdf:
                            total_pages = len(pdf.pages)
                            progress_bar = st.progress(0)
                            
                            for i, page in enumerate(pdf.pages):
                                status_text.info(f"⏳ Reading PDF Page {i+1} of {total_pages}...")
                                page_text = page.extract_text()
                                if page_text:
                                    full_raw_text += page_text + "\n"
                                progress_bar.progress((i + 1) / total_pages)
                        
                        status_text.info(f"⚙️ Parsing PDF data for {selected_bank}...")
                        all_parsed_entries = master_parser(selected_bank, full_raw_text)
                        
                        st.session_state.raw_text = full_raw_text[:3000] + "\n\n... (Preview limited) ..."
                        if os.path.exists(temp_pdf_path):
                            os.remove(temp_pdf_path)
                            
                    else:
                        status_text.info(f"⏳ Reading {file_ext.upper()} file... Please wait.")
                        
                        if file_ext == "csv":
                            df_raw = pd.read_csv(uploaded_file, on_bad_lines='skip', header=None)
                        else:
                            df_raw = pd.read_excel(uploaded_file, header=None)
                            
                        status_text.info(f"⚙️ Structuring tabular data for {selected_bank}...")
                        all_parsed_entries = master_excel_parser(selected_bank, df_raw)
                        
                        st.session_state.raw_text = f"Raw tabular data processed directly from {file_ext.upper()}."

                    status_text.success("✅ Extraction Complete! Scroll down to review.")
                    st.session_state.parsed_data = all_parsed_entries
                    
                except Exception as e:
                    error_msg = str(e).lower()
                    if is_encrypted and ("password" in error_msg or "decryption" in error_msg):
                        st.error("❌ Incorrect Password! Please enter the correct password.")
                    else:
                        st.error(f"Error reading file: {e}")
                finally:
                    if 'temp_pdf_path' in locals() and os.path.exists(temp_pdf_path):
                        os.remove(temp_pdf_path)

# ==========================================
# STEP 2: PREVIEW DATA (AUDIT MODE)
# ==========================================
if st.session_state.parsed_data is not None:
    st.markdown("---")
    total_entries = len(st.session_state.parsed_data)
    
    col_title, col_metric = st.columns([3, 1])
    with col_title:
        st.subheader("Step 2: Review Extracted Data (Audit Mode 🔍)")
    with col_metric:
        st.success(f"✅ Total Entries Found: **{total_entries}**")
    
    if total_entries > 0:
        df = pd.DataFrame(st.session_state.parsed_data)
        df.index = df.index + 1
        
        def clean_amt(val):
            if pd.isna(val) or str(val).strip() == "": return 0.0
            try: 
                clean_str = str(val).replace(',', '').replace('Cr', '').replace('Dr', '').replace(' ', '').strip()
                if clean_str.count('.') > 1:
                    parts = clean_str.split('.')
                    clean_str = "".join(parts[:-1]) + "." + parts[-1]
                return float(clean_str)
            except: 
                return 0.0

        is_error = [False] * len(df)
        for i in range(1, len(df)):
            prev_bal = clean_amt(df['Balance'].iloc[i-1])
            curr_dr  = clean_amt(df['Withdrawal (Dr)'].iloc[i])
            curr_cr  = clean_amt(df['Deposit (Cr)'].iloc[i])
            curr_bal = clean_amt(df['Balance'].iloc[i])
            expected_bal = prev_bal - curr_dr + curr_cr
            if abs(expected_bal - curr_bal) > 0.05:
                is_error[i] = True
                
        df['Recon_Error'] = is_error
        
        def highlight_errors(row):
            if row['Recon_Error']:
                return ['background-color: #ffe6e6; color: #b30000; font-weight: bold'] * len(row)
            return [''] * len(row)
            
        styled_df = df.style.apply(highlight_errors, axis=1)

        st.dataframe(
            styled_df, 
            use_container_width=True, 
            height=800,
            column_config={
                "Particulars / Narration": st.column_config.TextColumn(width="large"),
                "Recon_Error": None
            }
        )
        
        if any(is_error):
            st.error("⚠️ **Reconciliation Alert:** Calculation mismatch detected for some entries (highlighted in red). Please verify the amounts from the original document. An entry might have been skipped due to a page break.")
    else:
        st.warning(f"No transactions detected. Please ensure you selected the correct bank ({selected_bank}).")
        
        if st.query_params.get("admin") == "true":
            with st.expander("🛠️ Admin Only: Show Raw OCR Text (Debugging)"):
                st.text_area("", st.session_state.raw_text, height=200)

    # ==========================================
    # STEP 3: GENERATE XML & SMART LEDGER MAPPING
    # ==========================================
    if total_entries > 0:
        st.markdown("---")
        st.subheader("Step 3: Smart Ledger Mapping & XML")
        
        # 🌟 Rule Engine Database Management
        RULE_FILE = "ledger_rules.json"
        
        def load_rules():
            if os.path.exists(RULE_FILE):
                with open(RULE_FILE, "r") as f: return json.load(f)
            # 🌟 CONTRA वाउचर के लिए ATM और CASH का डिफ़ॉल्ट रूल
            return {"SBI CARDS": "Credit Card", "JIO": "Telephone Expenses", "ATM": "Cash", "CASH": "Cash"}
            
        def save_rules(r):
            with open(RULE_FILE, "w") as f: json.dump(r, f)
            
        if 'ledger_rules' not in st.session_state:
            st.session_state.ledger_rules = load_rules()

        # 🌟 UI FOR ADDING NEW RULES
        with st.expander("🧠 Setup Smart Ledger Rules (Keyword -> Ledger)", expanded=False):
            col_k, col_l, col_b = st.columns([2, 2, 1])
            with col_k:
                new_keyword = st.text_input("Narration Keyword (e.g. ZOMATO)", placeholder="Enter keyword...")
            with col_l:
                new_ledger = st.text_input("Tally Ledger Name (e.g. Office Expense)", placeholder="Enter ledger name...")
            with col_b:
                st.markdown("<br>", unsafe_allow_html=True) # spacing
                if st.button("➕ Add Rule"):
                    if new_keyword and new_ledger:
                        st.session_state.ledger_rules[new_keyword] = new_ledger
                        save_rules(st.session_state.ledger_rules)
                        st.success(f"Rule Added: {new_keyword} ➔ {new_ledger}")
                        st.rerun() 
                    else:
                        st.error("Fill both fields!")
                        
            # Show existing rules
            if st.session_state.ledger_rules:
                st.markdown("**Current Active Rules:**")
                for kw, ld in st.session_state.ledger_rules.items():
                    st.code(f"IF narration contains '{kw}' ➔ SET Ledger as '{ld}'")

        # ====================================================
        # 🌟 NEW FEATURE: DATE RANGE FILTER 📅
        # ====================================================
        st.markdown("### 📅 Filter Transactions by Date")
        
        # स्ट्रिंग (DD/MM/YYYY) को असली Python Date में बदलना
        def parse_dt(d_str):
            try: return datetime.strptime(d_str, "%d/%m/%Y").date()
            except: return None
        
        # डेटा में से सबसे पुरानी और सबसे नई तारीख ढूँढना
        all_dates = [parse_dt(e["Date"]) for e in st.session_state.parsed_data if parse_dt(e["Date"])]
        if all_dates:
            min_dt, max_dt = min(all_dates), max(all_dates)
        else:
            min_dt, max_dt = datetime.today().date(), datetime.today().date()

        # Streamlit Date Picker UI
        selected_dates = st.date_input(
            "Select Date Range for XML Generation:",
            value=(min_dt, max_dt),
            min_value=min_dt,
            max_value=max_dt,
            format="DD/MM/YYYY"
        )
        
        st.markdown("---")
        
        # 🌟 Tally Bank Ledger Input
        default_ledger_name = f"{selected_bank} Account"
        tally_ledger_name = st.text_input(
            "Tally Bank Ledger Name:", 
            value=default_ledger_name,
            help="Type the EXACT bank ledger name as created in your Tally ERP 9 / Prime"
        )
        
        # 🌟 Confirm Button & Dual Export (XML + Excel)
        if st.button("⚙️ Confirm & Generate Files"):
            with st.spinner("Generating Tally XML and Excel Files..."):
                try:
                    # 🌟 DATE FILTERING LOGIC
                    filtered_data = []
                    if len(selected_dates) == 2:
                        start_dt, end_dt = selected_dates
                        for entry in st.session_state.parsed_data:
                            edt = parse_dt(entry["Date"])
                            if edt is None or (start_dt <= edt <= end_dt):
                                filtered_data.append(entry)
                    else:
                        filtered_data = st.session_state.parsed_data

                    if len(filtered_data) == 0:
                        st.error("❌ चुनी गई तारीखों के बीच कोई ट्रांज़ैक्शन नहीं है!")
                    else:
                        # 1. XML GENERATION
                        tally_xml = generate_tally_xml(filtered_data, tally_ledger_name, st.session_state.ledger_rules)
                        
                        # 2. EXCEL GENERATION (In-Memory)
                        df_export = pd.DataFrame(filtered_data)
                        excel_buffer = io.BytesIO()
                        # हमने openpyxl इंजन का उपयोग किया है जो आपने पहले ही इंस्टॉल कर रखा है
                        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                            df_export.to_excel(writer, index=False, sheet_name='Bank_Data')
                        excel_data = excel_buffer.getvalue()
                        
                        st.success(f"✅ Successfully Generated for {len(filtered_data)} entries! 🚀")
                        
                        # 🌟 DUAL DOWNLOAD BUTTONS (UI Layout)
                        col_xml, col_excel = st.columns(2)
                        with col_xml:
                            st.download_button(
                                label=f"⬇️ Download {len(filtered_data)} Vouchers (.xml)",
                                data=tally_xml,
                                file_name=f"{tally_ledger_name.replace(' ', '_')}_tally.xml",
                                mime="application/xml",
                                use_container_width=True
                            )
                        with col_excel:
                            st.download_button(
                                label=f"📊 Download Excel Report (.xlsx)",
                                data=excel_data,
                                file_name=f"{tally_ledger_name.replace(' ', '_')}_report.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True
                            )
                except Exception as e:
                    st.error(f"Generation Error: {e}")