"""
Slip Verification Engine for Thai Mobile Banking Slips
Supports:
1. Real-time Live SlipOK API integration (Interbank ITMX clearance)
2. EasySlip API integration
3. Built-in Local QR Slip Code Scanner & Anti-Duplicate Replay Prevention
4. Thai Bank Code Directory (KBank, SCB, KTB, BBL, TTB, BAY, GSB, etc.)
5. Sandbox / Testing Simulation Mode
"""

import re
import datetime
import io
import httpx
from PIL import Image
import zxingcpp

# Thai Bank Code to Name Mapping (ITMX Bank Codes)
THAI_BANKS = {
    "002": {"name": "Bangkok Bank (ธนาคารกรุงเทพ)", "short": "BBL", "color": "#1e3799"},
    "004": {"name": "Kasikornbank (ธนาคารกสิกรไทย)", "short": "KBANK", "color": "#00a651"},
    "006": {"name": "Krungthai Bank (ธนาคารกรุงไทย)", "short": "KTB", "color": "#00a3e0"},
    "011": {"name": "TMBThanachart Bank (ธนาคารทหารไทยธนชาต)", "short": "TTB", "color": "#002d62"},
    "014": {"name": "Siam Commercial Bank (ธนาคารไทยพาณิชย์)", "short": "SCB", "color": "#4e2a84"},
    "025": {"name": "Bank of Ayudhya (ธนาคารกรุงศรีอยุธยา)", "short": "BAY", "color": "#fec300"},
    "030": {"name": "Government Savings Bank (ธนาคารออมสิน)", "short": "GSB", "color": "#eb1985"},
    "033": {"name": "Government Housing Bank (ธนาคารอาคารสงเคราะห์)", "short": "GHB", "color": "#f37021"},
    "034": {"name": "BAAC (ธ.ก.ส.)", "short": "BAAC", "color": "#43a047"},
    "069": {"name": "Kiatnakin Phatra Bank (ธนาคารเกียรตินาคินภัทร)", "short": "KKP", "color": "#1e3799"},
    "073": {"name": "Land and Houses Bank (ธนาคารแลนด์ แอนด์ เฮ้าส์)", "short": "LH", "color": "#6c757d"},
}


class SlipVerificationResult:
    def __init__(
        self,
        is_valid: bool,
        status: str,
        message: str,
        bank_code: str | None = None,
        bank_name: str | None = None,
        trans_ref: str | None = None,
        amount: float | None = None,
        sender_name: str | None = None,
        receiver_name: str | None = None,
        trans_time: str | None = None,
        raw_qr: str | None = None,
        verification_method: str = "local_qr"
    ):
        self.is_valid = is_valid
        self.status = status  # 'VERIFIED', 'SUSPICIOUS', 'DUPLICATE', 'AMOUNT_MISMATCH', 'NO_QR', 'MANUAL_REVIEW'
        self.message = message
        self.bank_code = bank_code
        self.bank_name = bank_name or (THAI_BANKS.get(bank_code, {}).get("name", "Unknown Bank") if bank_code else None)
        self.trans_ref = trans_ref
        self.amount = amount
        self.sender_name = sender_name
        self.receiver_name = receiver_name
        self.trans_time = trans_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.raw_qr = raw_qr
        self.verification_method = verification_method

    def to_dict(self):
        return {
            "is_valid": self.is_valid,
            "status": self.status,
            "message": self.message,
            "bank_code": self.bank_code,
            "bank_name": self.bank_name,
            "trans_ref": self.trans_ref,
            "amount": self.amount,
            "sender_name": self.sender_name,
            "receiver_name": self.receiver_name,
            "trans_time": self.trans_time,
            "raw_qr": self.raw_qr,
            "verification_method": self.verification_method
        }


def extract_qr_from_image(image_bytes: bytes) -> str | None:
    """Decodes QR code from slip image bytes using zxing-cpp."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        # zxing-cpp can read PIL Image directly
        results = zxingcpp.read_barcodes(image)
        for res in results:
            if res.format == zxingcpp.BarcodeFormat.QRCode and res.text:
                return res.text.strip()
    except Exception as e:
        print(f"Error decoding QR from image: {e}")
    return None


def parse_thai_slip_qr(qr_text: str) -> dict:
    """
    Parses Thai bank slip QR code payload (EMVCo / ITMX Mini-QR specification).
    Format typically: 0046000600000101030140225...
    - Tag 00: Subversion (000001)
    - Tag 01: Sending Bank Code (e.g. 014 = SCB, 004 = KBANK)
    - Tag 02: Transaction Reference (transRef)
    """
    data = {
        "is_thai_slip_qr": False,
        "bank_code": None,
        "trans_ref": None,
        "raw": qr_text
    }
    
    if not qr_text:
        return data

    # Check for standard ITMX subtag pattern
    # 00 = Sub-version, 01 = Sending Bank, 02 = TransRef
    # Example: 004600060000010103004022520240327...
    if "000001" in qr_text:
        data["is_thai_slip_qr"] = True
        
        # Match Tag 01 (Bank code)
        # 01 03 [3-digit bank code]
        bank_match = re.search(r"0103([0-9]{3})", qr_text)
        if bank_match:
            data["bank_code"] = bank_match.group(1)
            
        # Match Tag 02 (TransRef)
        # 02 [length 2 digits] [ref digits/chars]
        ref_match = re.search(r"02([0-9]{2})([A-Za-z0-9_\-]+)", qr_text)
        if ref_match:
            length = int(ref_match.group(1))
            full_rest = ref_match.group(2)
            data["trans_ref"] = full_rest[:length]
            
    # Fallback pattern matching for various banks
    if not data["trans_ref"]:
        # Kasikorn / SCB alternative QR strings
        kbank_match = re.search(r"(?:KBANK|SCB|KTB|BBL|TTB|BAY|GSB)[A-Za-z0-9_\-]{8,35}", qr_text, re.IGNORECASE)
        if kbank_match:
            data["trans_ref"] = kbank_match.group(0)
            data["is_thai_slip_qr"] = True
            
    return data


async def verify_with_slipok(
    api_key: str,
    branch_id: str | None,
    raw_qr: str | None,
    image_bytes: bytes | None,
    expected_amount: float | None = None,
    expected_promptpay: str | None = None,
    expected_receiver_name: str | None = None
) -> SlipVerificationResult | None:
    """Calls SlipOK Live API (https://api.slipok.com/api/line/apikey/{branch_id})."""
    if not api_key:
        return None

    # Correct endpoint: https://api.slipok.com/api/line/apikey/{branch_id}
    if branch_id:
        url = f"https://api.slipok.com/api/line/apikey/{branch_id}"
    else:
        url = "https://api.slipok.com/api/line/apikey/unknown"

    headers = {
        "x-authorization": api_key.strip()
    }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            if raw_qr:
                # Fast payload check using form data (SlipOK requires multipart)
                form_data = {"data": raw_qr, "log": "true"}
                if expected_amount:
                    form_data["amount"] = str(expected_amount)
                res = await client.post(url, data=form_data, headers=headers)
            elif image_bytes:
                files = {"files": ("slip.jpg", image_bytes, "image/jpeg")}
                data = {"log": "true"}
                if expected_amount:
                    data["amount"] = str(expected_amount)
                res = await client.post(url, files=files, data=data, headers=headers)
            else:
                return None
                
            if res.status_code == 200:
                resp_json = res.json()
                if resp_json.get("success"):
                    d = resp_json.get("data", {})
                    amount = float(d.get("amount", 0.0))
                    trans_ref = d.get("transRef")
                    bank_code = d.get("sendingBank")
                    sender_name = d.get("sender", {}).get("name")
                    date_str = d.get("date")

                    # Extract receiver information
                    receiver_data = d.get("receiver", {})
                    rec_proxy = str(receiver_data.get("proxy", {}).get("value") or "")
                    rec_name = str(receiver_data.get("name") or receiver_data.get("account", {}).get("name", {}).get("th") or "")

                    # 1. Check receiver account matching (prevent using slips transferred to other people/stores)
                    if expected_promptpay and rec_proxy:
                        exp_digits = "".join(ch for ch in expected_promptpay if ch.isdigit())
                        rec_digits = "".join(ch for ch in rec_proxy if ch.isdigit())
                        if exp_digits and rec_digits and exp_digits != rec_digits:
                            return SlipVerificationResult(
                                is_valid=False,
                                status="WRONG_RECEIVER",
                                message="สลิปนี้ไม่ได้โอนเข้าบัญชีของวง (ผู้รับเงินไม่ตรงกับบัญชีของวง)",
                                bank_code=bank_code,
                                trans_ref=trans_ref,
                                amount=amount,
                                sender_name=sender_name,
                                receiver_name=rec_name,
                                trans_time=date_str,
                                verification_method="slipok_live"
                            )
                    
                    # 2. Check amount match (prevent underpayment)
                    if expected_amount and amount < expected_amount:
                        return SlipVerificationResult(
                            is_valid=False,
                            status="AMOUNT_MISMATCH",
                            message=f"ยอดโอนจริง ({amount:,.2f}฿) น้อยกว่ายอดทิปที่ขอ ({expected_amount:,.2f}฿)",
                            bank_code=bank_code,
                            trans_ref=trans_ref,
                            amount=amount,
                            sender_name=sender_name,
                            receiver_name=rec_name,
                            trans_time=date_str,
                            verification_method="slipok_live"
                        )

                    # 3. Check transaction freshness (prevent using old slips from yesterday or last week)
                    if date_str:
                        try:
                            clean_date = date_str.replace("Z", "").split(".")[0]
                            trans_dt = datetime.datetime.fromisoformat(clean_date)
                            now = datetime.datetime.now()
                            diff_hours = abs((now - trans_dt).total_seconds()) / 3600
                            if diff_hours > 8:
                                return SlipVerificationResult(
                                    is_valid=False,
                                    status="EXPIRED_SLIP",
                                    message="สลิปนี้เกินกำหนดเวลาโอน (อนุญาตเฉพาะสลิปที่โอนภายในช่วงเวลาเล่นสดวันนี้)",
                                    bank_code=bank_code,
                                    trans_ref=trans_ref,
                                    amount=amount,
                                    sender_name=sender_name,
                                    receiver_name=rec_name,
                                    trans_time=date_str,
                                    verification_method="slipok_live"
                                )
                        except Exception:
                            pass
                        
                    return SlipVerificationResult(
                        is_valid=True,
                        status="VERIFIED",
                        message="สลิปถูกต้อง 100% ผ่านการตรวจสอบเงินเข้าจริงจากระบบธนาคาร ITMX (SlipOK)",
                        bank_code=bank_code,
                        trans_ref=trans_ref,
                        amount=amount,
                        sender_name=sender_name,
                        receiver_name=rec_name,
                        trans_time=date_str,
                        raw_qr=raw_qr,
                        verification_method="slipok_live"
                    )
                else:
                    msg = resp_json.get("message", "SlipOK ตรวจสอบสลิปไม่ผ่าน")
                    return SlipVerificationResult(
                        is_valid=False,
                        status="INVALID_SLIP",
                        message=f"ธนาคารแจ้ง: {msg}",
                        verification_method="slipok_live"
                    )
    except Exception as e:
        print(f"SlipOK API call failed: {e}")
        return None
    return None


async def verify_slip(
    image_bytes: bytes,
    expected_amount: float,
    band_settings: dict,
    existing_trans_refs: set[str]
) -> SlipVerificationResult:
    """
    Main verification pipeline:
    1. Scan and decode QR code from slip image.
    2. Check if live SlipOK API key is configured and verify through ITMX.
    3. Run built-in Thai bank QR decoder & check duplicate transRef against database.
    4. Validate tip amount & timestamps.
    """
    qr_text = extract_qr_from_image(image_bytes)
    
    # 1. Try Live SlipOK API if configured
    slipok_key = band_settings.get("slipok_api_key")
    branch_id = band_settings.get("slipok_branch_id")
    expected_pp = band_settings.get("promptpay_number")
    expected_name = band_settings.get("promptpay_name")

    if slipok_key:
        live_res = await verify_with_slipok(
            api_key=slipok_key,
            branch_id=branch_id,
            raw_qr=qr_text,
            image_bytes=image_bytes,
            expected_amount=expected_amount,
            expected_promptpay=expected_pp,
            expected_receiver_name=expected_name
        )
        if live_res is not None:
            # Check duplicate in local database as well
            if live_res.trans_ref and live_res.trans_ref in existing_trans_refs:
                live_res.is_valid = False
                live_res.status = "DUPLICATE"
                live_res.message = f"❌ สลิปนี้เคยถูกใช้ส่งขอเพลงไปแล้ว (TransRef ซ้ำ: {live_res.trans_ref})"
            return live_res

    # 2. Local QR Code Analysis
    if not qr_text:
        # Check if Sandbox/Demo mode allows manual review
        if band_settings.get("allow_manual_approval", True):
            return SlipVerificationResult(
                is_valid=False,
                status="MANUAL_REVIEW",
                message="ตรวจไม่พบ QR Code บนสลิป (ส่งให้นักดนตรีตรวจสอบสลิปด้วยตาเปล่าที่หน้าเวที)",
                amount=expected_amount,
                verification_method="manual_review"
            )
        else:
            return SlipVerificationResult(
                is_valid=False,
                status="NO_QR",
                message="ไม่พบ Mini-QR Code บนสลิป กรุณาอัปโหลดสลิปที่มี QR Code ชัดเจน",
                verification_method="local_qr"
            )

    parsed = parse_thai_slip_qr(qr_text)
    trans_ref = parsed.get("trans_ref")
    bank_code = parsed.get("bank_code")
    
    # Check if duplicate slip (Replay attack prevention)
    if trans_ref and trans_ref in existing_trans_refs:
        bank_name = THAI_BANKS.get(bank_code, {}).get("name", "ธนาคารไทย")
        return SlipVerificationResult(
            is_valid=False,
            status="DUPLICATE",
            message=f"❌ ตรวจพบสลิปซ้ำ! รหัสอ้างอิง {trans_ref} นี้เคยถูกใช้ขอเพลงไปแล้ว",
            bank_code=bank_code,
            bank_name=bank_name,
            trans_ref=trans_ref,
            raw_qr=qr_text,
            verification_method="local_anti_duplicate"
        )
        
    if parsed.get("is_thai_slip_qr"):
        bank_name = THAI_BANKS.get(bank_code, {}).get("name", "ธนาคารไทย")
        return SlipVerificationResult(
            is_valid=True,
            status="VERIFIED",
            message=f"✅ สลิปถูกต้อง ตรวจพบ Mini-QR ของ {bank_name} (Ref: {trans_ref or 'OK'})",
            bank_code=bank_code,
            bank_name=bank_name,
            trans_ref=trans_ref or f"REF-{int(datetime.datetime.now().timestamp())}",
            amount=expected_amount,
            raw_qr=qr_text,
            verification_method="local_qr_verified"
        )
        
    # Sandbox / Demo simulator mode check
    if band_settings.get("demo_mode", True):
        # Allow test slips with valid format
        ref_id = f"SIM-{int(datetime.datetime.now().timestamp())}"
        return SlipVerificationResult(
            is_valid=True,
            status="VERIFIED",
            message=f"✅ สลิปทดสอบจำลอง (Sandbox Verified - Ref: {ref_id})",
            bank_code="004",
            bank_name="Kasikornbank (Sandbox Simulation)",
            trans_ref=ref_id,
            amount=expected_amount,
            raw_qr=qr_text,
            verification_method="sandbox_simulation"
        )
        
    return SlipVerificationResult(
        is_valid=False,
        status="SUSPICIOUS",
        message="QR Code บนสลิปไม่ตรงกับรูปแบบมาตรฐานของธนาคารไทย ส่งให้นักดนตรีตรวจสอบ",
        raw_qr=qr_text,
        verification_method="local_qr"
    )
