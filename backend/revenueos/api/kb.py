from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import KBDocument
from ..tools import kb

router = APIRouter(prefix="/api/kb", tags=["knowledge"])


class DocIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    category: str
    content: str = Field(min_length=1)


def doc_to_dict(d: KBDocument) -> dict:
    return {"id": d.id, "title": d.title, "category": d.category, "content": d.content,
            "chunks": len(d.chunks), "created_at": d.created_at}


@router.get("")
def list_docs(db: Session = Depends(get_db)):
    return {"categories": kb.CATEGORIES,
            "documents": [doc_to_dict(d) for d in db.scalars(select(KBDocument).order_by(KBDocument.category, KBDocument.id))]}


@router.post("")
def create_doc(doc: DocIn, db: Session = Depends(get_db)):
    try:
        d = kb.add_document(db, doc.title, doc.category, doc.content)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.commit()
    return doc_to_dict(d)


@router.post("/upload")
async def upload_doc(file: UploadFile, category: str = Form(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith((".txt", ".md")):
        raise HTTPException(415, "Upload .txt or .md files (paste other formats as text).")
    content = (await file.read()).decode("utf-8", errors="replace")
    try:
        d = kb.add_document(db, file.filename.rsplit(".", 1)[0], category, content)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.commit()
    return doc_to_dict(d)


@router.delete("/{doc_id}")
def delete_doc(doc_id: int, db: Session = Depends(get_db)):
    d = db.get(KBDocument, doc_id)
    if not d:
        raise HTTPException(404, "Document not found")
    db.delete(d)
    db.commit()
    return {"deleted": doc_id}


@router.get("/search")
def search(q: str, category: str | None = None, db: Session = Depends(get_db)):
    return kb.search(db, q, k=8, category=category)
