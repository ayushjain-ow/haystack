from pathlib import Path
from typing import List, Optional, Tuple, Union
import math
import bisect
from haystack.preview import component, Document, ExtractedAnswer
from haystack.preview.lazy_imports import LazyImport

with LazyImport(message="Run 'pip install farm-haystack[inference]'") as torch_and_transformers_import:
    from transformers import AutoModelForQuestionAnswering, AutoTokenizer
    from tokenizers import Encoding
    import torch


@component
class ExtractiveReader:
    """
    A component for performing extractive QA
    """

    def __init__(
        self,
        model: Union[Path, str] = "deepset/roberta-base-squad2-distilled",
        device: Optional[str] = None,
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
        max_seq_length: int = 384,
        stride: int = 128,
        max_batch_size: Optional[int] = None,
        answers_per_seq: Optional[int] = None,
        no_answer: bool = True,
        calibration_factor: float = 0.1,
    ) -> None:
        """
        Creates an ExtractiveReader
        :param model: A HuggingFace transformers question answering model.
            Can either be a path to a folder containing the model files or an identifier for the HF hub
            Default: `'deepset/roberta-base-squad2-distilled'`
        :param device: Pytorch device string. Uses GPU by default if available
        :param top_k: Number of answers to return per query.
            If neither top_k nor top_p is set by the user, top_k will default to 10
        :param top_p: Probability mass that should be contained in returned samples (nucleus sampling)
        :param max_seq_length: Maximum number of tokens.
            If exceeded by a sequence, the sequence will be split.
            Default: 384
        :param stride: Number of tokens that overlap when sequence is split because it exceeds max_seq_length
            Default: 128
        :param max_batch_size: Maximum number of samples that are fed through the model at the same time
        :param answers_per_seq: Number of answer candidates to consider per sequence.
            This is relevant when a document has been split into multiple sequence due to max_seq_length.
        :param no_answer: Whether to return no answer scores
        :param calibration_factor: Factor used for calibrating confidence scores
        """
        torch_and_transformers_import.check()
        self.model = str(model)
        self.model_ = None
        self.device = device
        self.max_seq_length = max_seq_length
        self.top_k = top_k
        self.top_p = top_p
        self.stride = stride
        self.max_batch_size = max_batch_size
        self.answers_per_seq = answers_per_seq
        self.no_answer = no_answer
        self.calibration_factor = calibration_factor

    def warm_up(self):
        if self.model_ is None:
            if torch.cuda.is_available():
                self.device = self.device or "cuda:0"
            else:
                self.device = self.device or "cpu:0"
            self.model_ = AutoModelForQuestionAnswering.from_pretrained(self.model).to(self.device)
            self.tokenizer = AutoTokenizer.from_pretrained(self.model)

    def _flatten(
        self, queries: List[str], documents: List[List[Document]]
    ) -> Tuple[List[str], List[Document], List[int]]:
        flattened_queries = [query for documents_, query in zip(documents, queries) for _ in documents_]
        flattened_documents = [document for documents_ in documents for document in documents_]
        query_ids = [i for i, documents_ in enumerate(documents) for _ in documents_]
        return flattened_queries, flattened_documents, query_ids

    def _preprocess(
        self, queries: List[str], documents: List[Document], max_seq_length: int, query_ids: List[int], stride: int
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, List[Encoding], List[int], List[int]]:
        encodings = self.tokenizer(
            queries,
            [document.content for document in documents],
            padding=True,
            truncation=True,
            max_length=max_seq_length,
            return_tensors="pt",
            return_overflowing_tokens=True,
            stride=stride,
        )

        input_ids = encodings.input_ids.to(self.device)
        attention_mask = encodings.attention_mask.to(self.device)

        query_ids = [query_ids[index] for index in encodings.overflow_to_sample_mapping]
        document_ids = encodings.overflow_to_sample_mapping

        encodings = encodings.encodings
        sequence_ids = torch.tensor(
            [[id_ if id_ is not None else -1 for id_ in encoding.sequence_ids] for encoding in encodings]
        ).to(self.device)

        return input_ids, attention_mask, sequence_ids, encodings, query_ids, document_ids

    def _postprocess(
        self,
        start: torch.Tensor,
        end: torch.Tensor,
        sequence_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        answers_per_seq: int,
        encodings: List[Encoding],
    ) -> Tuple[List[List[Optional[int]]], List[List[Optional[int]]], torch.Tensor]:
        mask = sequence_ids == 1
        mask = torch.logical_and(mask, attention_mask == 1)
        start = torch.where(mask, start, -torch.inf)
        end = torch.where(mask, end, -torch.inf)
        start = start.unsqueeze(-1)
        end = end.unsqueeze(-2)

        logits = start + end  # shape: (batch_size, seq_length (start), seq_length (end))
        mask = torch.ones(logits.shape[-2:], dtype=torch.bool, device=self.device)
        mask = torch.triu(mask)  # End shouldn't be before start
        mask[0, :] = False  # TODO: Might not be necessary because of sequence id
        masked_logits = torch.where(mask, logits, -torch.inf)
        probabilities = torch.sigmoid(masked_logits * self.calibration_factor)

        flat_probabilities = probabilities.flatten(-2, -1)  # necessary for topk
        candidates = torch.topk(flat_probabilities, answers_per_seq)
        seq_length = logits.shape[-1]
        start_candidates = candidates.indices // seq_length  # Recover indices from flattening
        end_candidates = candidates.indices % seq_length
        start_candidates = start_candidates.cpu()
        end_candidates = end_candidates.cpu()

        start_candidates = [
            [encoding.token_to_chars(start)[0] if start != 0 else None for start in candidates]
            for candidates, encoding in zip(start_candidates, encodings)
        ]
        end_candidates = [
            [encoding.token_to_chars(end)[1] if end != 0 else None for end in candidates]
            for candidates, encoding in zip(end_candidates, encodings)
        ]
        probabilities = candidates.values.cpu()

        return start_candidates, end_candidates, probabilities

    def _unflatten(
        self,
        start: List[List[Optional[int]]],
        end: List[List[Optional[int]]],
        probabilities: torch.Tensor,
        flattened_documents: List[Document],
        queries: List[str],
        answers_per_seq: int,
        top_k: Optional[int],
        top_p: Optional[float],
        query_ids: List[int],
        document_ids: List[int],
        no_answer: bool,
    ) -> List[List[ExtractedAnswer]]:
        flat_answers_without_queries: List[Tuple[Document, Optional[str], float, Optional[int], Optional[int]]] = [
            (doc := flattened_documents[document_id], doc.content[start:end], probability.item(), start, end)
            if start is not None and end is not None
            else (flattened_documents[document_id], None, probability.item(), None, None)
            for document_id, start_candidates_, end_candidates_, probabilities_ in zip(
                document_ids, start, end, probabilities
            )
            for start, end, probability in zip(start_candidates_, end_candidates_, probabilities_)
        ]

        i = 0
        nested_answers = []
        for query_id in range(query_ids[-1] + 1):
            current_answers = []
            while i < len(flat_answers_without_queries) and query_ids[i // answers_per_seq] == query_id:
                doc, data, probability, cur_start, cur_end = flat_answers_without_queries[i]
                answer = ExtractedAnswer(
                    data=data,
                    question=queries[query_id],
                    metadata={},
                    document=doc,
                    probability=probability,
                    start=cur_start,
                    end=cur_end,
                )
                current_answers.append(answer)
                i += 1
            current_answers = sorted(current_answers, key=lambda answer: answer.probability, reverse=True)
            if top_k is not None:
                current_answers = current_answers[:top_k]
            if no_answer:
                no_answer_probability = math.prod(1 - answer.probability for answer in current_answers)
                answer = ExtractedAnswer(
                    data=None, question=queries[query_id], metadata={}, document=None, probability=no_answer_probability
                )
                bisect.insort(current_answers, answer, key=lambda answer: -answer.probability)
            if top_p is not None:
                p_sum = 0.0
                for i, answer in enumerate(current_answers):
                    p_sum += answer.probability
                    if p_sum >= top_p:
                        current_answers = current_answers[: i + 1]
                        break
            nested_answers.append(current_answers)

        return nested_answers

    @component.output_types(answers=List[List[ExtractedAnswer]])
    def run(
        self,
        queries: List[str],
        documents: List[List[Document]],
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
        max_seq_length: Optional[int] = None,
        stride: Optional[int] = None,
        max_batch_size: Optional[int] = None,
        answers_per_seq: Optional[int] = None,
        no_answer: Optional[bool] = None,
    ):
        top_k = top_k or self.top_k
        top_p = top_p or self.top_p
        if top_k is None and top_p is None:
            top_k = 10
        max_seq_length = max_seq_length or self.max_seq_length
        stride = stride or self.stride
        max_batch_size = max_batch_size or self.max_batch_size
        answers_per_seq = answers_per_seq or self.answers_per_seq or top_k or 20
        no_answer = no_answer or self.no_answer

        flattened_queries, flattened_documents, query_ids = self._flatten(queries, documents)
        input_ids, attention_mask, sequence_ids, encodings, query_ids, document_ids = self._preprocess(
            flattened_queries, flattened_documents, max_seq_length, query_ids, stride
        )

        num_batches = math.ceil(input_ids.shape[0] / max_batch_size) if max_batch_size else 1
        batch_size = max_batch_size or input_ids.shape[0]

        start_logits_list = []
        end_logits_list = []

        for i in range(num_batches):
            start_index = i * batch_size
            end_index = start_index + batch_size
            cur_input_ids = input_ids[start_index:end_index]
            cur_attention_mask = attention_mask[start_index:end_index]

            output = self.model_(input_ids=cur_input_ids, attention_mask=cur_attention_mask)  # type: ignore # we know that self._model can't be None
            cur_start_logits = output.start_logits
            cur_end_logits = output.end_logits
            if num_batches != 1:
                cur_start_logits = cur_start_logits.cpu()
                cur_end_logits = cur_end_logits.cpu()
            start_logits_list.append(cur_start_logits)
            end_logits_list.append(cur_end_logits)

        start_logits = torch.cat(start_logits_list)
        end_logits = torch.cat(end_logits_list)

        start, end, probabilities = self._postprocess(
            start_logits, end_logits, sequence_ids, attention_mask, answers_per_seq, encodings
        )

        answers = self._unflatten(
            start,
            end,
            probabilities,
            flattened_documents,
            queries,
            answers_per_seq,
            top_k,
            top_p,
            query_ids,
            document_ids,
            no_answer,
        )

        return {"answers": answers}
