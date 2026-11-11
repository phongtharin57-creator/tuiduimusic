"""
Thai PromptPay QR Code Generator (EMVCo Standard)
Generates valid Thai QR Payment payloads with CRC-16 calculation.
Supports: Mobile Phone (08x, 09x, 06x), Citizen ID (13 digits), and E-Wallet IDs.
"""

def _crc16(data: str) -> str:
    """Calculates CRC-16/CCITT-FALSE checksum for EMVCo QR code."""
    crc = 0xFFFF
    for char in data.encode('ascii'):
        crc ^= (char << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def format_emv_tag(tag: str, value: str) -> str:
    """Formats a TLV (Tag-Length-Value) string."""
    length = f"{len(value):02d}"
    return f"{tag}{length}{value}"


def sanitize_promptpay_target(target: str) -> str:
    """Sanitizes PromptPay target (phone or national ID)."""
    digits = "".join(ch for ch in target if ch.isdigit())
    if len(digits) == 10 and digits.startswith("0"):
        # Mobile number: 0812345678 -> 0066812345678
        return "0066" + digits[1:]
    elif len(digits) == 9 and not digits.startswith("0"):
        # Mobile without leading 0
        return "0066" + digits
    return digits


def generate_promptpay_payload(target: str, amount: float | None = None) -> str:
    """
    Generates EMVCo QR code payload for PromptPay.
    :param target: Thai Phone number (0812345678) or National ID (13 digits)
    :param amount: Optional amount in THB (e.g. 100.00)
    :return: EMVCo formatted string
    """
    sanitized = sanitize_promptpay_target(target)
    
    # Payload format indicator (01)
    tag00 = format_emv_tag("00", "01")
    
    # Point of Initiation: 11 = Static (no amount or reusable), 12 = Dynamic (with specific amount)
    point_of_init = "12" if (amount is not None and amount > 0) else "11"
    tag01 = format_emv_tag("01", point_of_init)
    
    # Merchant Account Information (Tag 29 for PromptPay)
    # Subtag 00: AID for PromptPay = A000000677010111
    sub00 = format_emv_tag("00", "A000000677010111")
    if len(sanitized) == 15:
        # E-Wallet / Biller ID / KBank Merchant (Subtag 03)
        sub03 = format_emv_tag("03", sanitized)
        tag29_val = sub00 + sub03
    elif len(sanitized) == 13:
        # National ID or Tax ID (Subtag 02)
        sub02 = format_emv_tag("02", sanitized)
        tag29_val = sub00 + sub02
    else:
        # Mobile phone (Subtag 01)
        sub01 = format_emv_tag("01", sanitized)
        tag29_val = sub00 + sub01
    tag29 = format_emv_tag("29", tag29_val)
    
    # Country code: TH
    tag58 = format_emv_tag("58", "TH")
    
    # Currency: THB (764)
    tag53 = format_emv_tag("53", "764")
    
    # Amount (Tag 54)
    tag54 = ""
    if amount is not None and amount > 0:
        tag54 = format_emv_tag("54", f"{amount:.2f}")
        
    raw_payload = f"{tag00}{tag01}{tag29}{tag53}{tag54}{tag58}6304"
    crc = _crc16(raw_payload)
    return raw_payload + crc
