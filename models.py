from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from database import Base


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    contact_lists = relationship("ContactList", back_populates="user", cascade="all, delete-orphan")
    campaigns = relationship("Campaign", back_populates="user", cascade="all, delete-orphan")
    waha_settings = relationship("WAHASettings", back_populates="user", uselist=False, cascade="all, delete-orphan")


class ContactList(Base):
    __tablename__ = "contact_lists"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User", back_populates="contact_lists")
    contacts = relationship("Contact", back_populates="contact_list", cascade="all, delete-orphan")
    campaign_lists = relationship("CampaignList", back_populates="contact_list", cascade="all, delete-orphan")


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True)
    list_id = Column(Integer, ForeignKey("contact_lists.id"), nullable=False)
    name = Column(String(200), nullable=False)
    phone = Column(String(50), nullable=False)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    contact_list = relationship("ContactList", back_populates="contacts")
    message_logs = relationship("MessageLog", back_populates="contact", cascade="all, delete-orphan")


class WAHASettings(Base):
    __tablename__ = "waha_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    base_url = Column(String(500), default="http://localhost:3000")
    api_key = Column(String(256), nullable=True)
    session_name = Column(String(100), default="default")

    user = relationship("User", back_populates="waha_settings")


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    message_template = Column(Text, nullable=False)
    status = Column(String(20), default="draft")  # draft, sending, completed, failed
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    sent_at = Column(DateTime, nullable=True)
    total_contacts = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)

    user = relationship("User", back_populates="campaigns")
    lists = relationship("CampaignList", back_populates="campaign", cascade="all, delete-orphan")
    logs = relationship("MessageLog", back_populates="campaign", cascade="all, delete-orphan")


class CampaignList(Base):
    __tablename__ = "campaign_lists"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False)
    list_id = Column(Integer, ForeignKey("contact_lists.id"), nullable=False)

    campaign = relationship("Campaign", back_populates="lists")
    contact_list = relationship("ContactList", back_populates="campaign_lists")


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    phone = Column(String(50))
    contact_name = Column(String(200))
    message = Column(Text)
    status = Column(String(20), default="pending")  # pending, sent, failed
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True)

    campaign = relationship("Campaign", back_populates="logs")
    contact = relationship("Contact", back_populates="message_logs")
