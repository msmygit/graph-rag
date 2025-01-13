import asyncio
from typing import (
    Any,
    Iterable,
    List,
    Sequence,
    Tuple,
    Union,
)

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field, computed_field

from .edge import Edge
from .edge_helper import EdgeHelper
from .node import Node
from .node_selectors.node_selector import NodeSelectorFactory
from .traversal_adapters.generic.base import METADATA_EMBEDDING_KEY, StoreAdapter

INFINITY = float("inf")


class _TraversalState:
    """Manages the book-keeping necessary to keep track of traversal state."""

    def __init__(
        self,
        *,
        k: int,
        node_selector_factory: NodeSelectorFactory,
        query_embedding: list[float],
        edge_helper: EdgeHelper,
        max_depth: int | None = None,
        **kwargs: dict[str, Any],
    ) -> None:
        self.k = k
        self.node_selector = node_selector_factory(
            k=k,
            query_embedding=query_embedding,
            **kwargs,
        )
        self.edge_helper = edge_helper
        self._max_depth = max_depth

        self.visited_edges: set[Edge] = set()
        self.edge_depths: dict[Edge, int] = {}
        self.doc_cache: dict[str, Document] = {}
        self.node_cache: dict[str, Node] = {}

        self.selected_nodes: dict[str, Node] = {}

    def _doc_to_new_node(
        self, doc: Document, *, depth: int | None = None
    ) -> Node | None:
        if doc.id is None:
            raise ValueError("All documents should have ids")
        if doc.id in self.node_cache:
            return None

        doc = self.doc_cache.setdefault(doc.id, doc)
        assert doc.id is not None
        incoming_edges, outgoing_edges = self.edge_helper.get_incoming_outgoing(
            doc.metadata
        )
        if depth is None:
            depth = min(
                [
                    d
                    for e in incoming_edges
                    if (d := self.edge_depths.get(e, None)) is not None
                ],
                default=0,
            )
        node = Node(
            id=doc.id,
            depth=depth,
            embedding=doc.metadata[METADATA_EMBEDDING_KEY],
            metadata=doc.metadata,
            incoming_edges=incoming_edges,
            outgoing_edges=outgoing_edges,
        )
        self.node_cache[doc.id] = node

        return node

    def add_docs(
        self, docs: Iterable[Document], *, depth: int | None = None
    ) -> dict[str, Node]:
        # Record the depth of new nodes.
        nodes = {
            node.id: node
            for doc in docs
            if (node := self._doc_to_new_node(doc, depth=depth)) is not None
            if (self._max_depth is None or node.depth <= self._max_depth)
        }
        self.node_selector.add_nodes(nodes)
        return nodes

    def visit_nodes(self, nodes: Iterable[Node]) -> set[Edge]:
        """Record the nodes as visited, returning the new outgoing edges.

        After this call, the outgoing edges will be added to the visited
        set, and not revisited during the traversal.
        """
        new_outgoing_edges: dict[Edge, int] = {}
        for node in nodes:
            node_new_outgoing_edges = node.outgoing_edges - self.visited_edges
            for edge in node_new_outgoing_edges:
                depth = new_outgoing_edges.setdefault(edge, node.depth + 1)
                if node.depth + 1 < depth:
                    new_outgoing_edges[edge] = node.depth + 1

        self.edge_depths.update(new_outgoing_edges)

        new_outgoing_edge_set = set(new_outgoing_edges.keys())
        self.visited_edges.update(new_outgoing_edge_set)
        return new_outgoing_edge_set

    def select_next_edges(self) -> set[Edge] | None:
        """Select the next round of nodes.

        Returns the set of new edges that need to be explored.
        """
        remaining = self.k - len(self.selected_nodes)

        if remaining <= 0:
            return None

        next_nodes = self.node_selector.select_nodes(limit=remaining)
        if not next_nodes:
            return None

        next_nodes = [n for n in next_nodes if n.id not in self.selected_nodes]
        if len(next_nodes) == 0:
            return None

        self.selected_nodes.update({n.id: n for n in next_nodes})
        new_outgoing_edges = self.visit_nodes(next_nodes)
        return new_outgoing_edges

    def finish(self) -> list[Document]:
        final_nodes = self.node_selector.finalize_nodes(self.selected_nodes.values())
        docs = []
        for node in final_nodes:
            doc = self.doc_cache.get(node.id, None)
            if doc is None:
                raise RuntimeError(
                    f"unexpected, cache should contain doc id: {node.id}"
                )
            # Compute new metadata from extra metadata and metadata.
            # This allows us to avoid modifying the orginal metadata.
            metadata = {
                "depth": node.depth,
                **node.extra_metadata,
                **doc.metadata,
            }
            # Remove the metadata embedding key. TODO: Find a better way to do this.
            metadata.pop(METADATA_EMBEDDING_KEY, None)
            docs.append(
                Document(
                    id=node.id,
                    page_content=doc.page_content,
                    metadata=metadata,
                )
            )
        return docs


# this class uses pydantic, so store and edges
# must be provided at init time.
class GenericGraphTraversalRetriever(BaseRetriever):
    store: StoreAdapter
    edges: List[Union[str, Tuple[str, str]]]
    node_selector_factory: NodeSelectorFactory

    k: int = Field(default=4)
    start_k: int = Field(default=100)
    adjacent_k: int = Field(default=10)
    use_denormalized_metadata: bool = Field(default=False)
    denormalized_path_delimiter: str = Field(default=".")
    denormalized_static_value: Any = Field(default=True)

    extra_args: dict[str, Any] = {}

    @computed_field  # type: ignore
    @property
    def edge_helper(self) -> EdgeHelper:
        return EdgeHelper(
            edges=self.edges,
            use_denormalized_metadata=self.use_denormalized_metadata,
            denormalized_path_delimiter=self.denormalized_path_delimiter,
            denormalized_static_value=self.denormalized_static_value,
        )

    def _get_relevant_documents(
        self,
        query: str,
        *,
        initial_roots: Sequence[str] = (),
        k: int | None = None,
        start_k: int | None = None,
        adjacent_k: int | None = None,
        filter: dict[str, Any] | None = None,
        store_kwargs: dict[str, Any] = {},
        **kwargs: Any,
    ) -> list[Document]:
        """Retrieve document nodes from this graph vector store using MMR-traversal.
        This strategy first retrieves the top `start_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.
        At each step, it considers the (remaining) documents from `start_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").
        Args:
            query: The query string to search for.
            initial_roots: Optional list of document IDs to use for initializing search.
                The top `adjacent_k` nodes adjacent to each initial root will be
                included in the set of initial candidates. To fetch only in the
                neighborhood of these nodes, set `start_k = 0`.
            k: Number of Documents to return. Defaults to 4.
            start_k: Number of initial Documents to fetch via similarity.
                Will be added to the nodes adjacent to `initial_roots`.
                Defaults to 100.
            adjacent_k: Number of adjacent Documents to fetch.
                Defaults to 10.
            filter: Optional metadata to filter the results.
            store_kwargs: Optional kwargs passed to queries to the store.
            **kwargs: Additional keyword arguments passed to traversal state.
        """
        k = self.k if k is None else k
        start_k = self.start_k if start_k is None else start_k
        adjacent_k = self.adjacent_k if adjacent_k is None else adjacent_k

        # Retrieve initial candidates and initialize state.
        query_embedding, initial_docs = self._fetch_initial_candidates(
            query, fetch_k=start_k, filter=filter, **store_kwargs
        )
        state = _TraversalState(
            k=k,
            node_selector_factory=self.node_selector_factory,
            query_embedding=query_embedding,
            edge_helper=self.edge_helper,
            **{**self.extra_args, **kwargs},
        )
        state.add_docs(initial_docs, depth=0)

        if initial_roots:
            neighborhood_adjacent_docs = self._fetch_neighborhood_candidates(
                initial_roots,
                query_embedding=query_embedding,
                adjacent_k=adjacent_k,
                state=state,
                filter=filter,
                **store_kwargs,
            )
            state.add_docs(neighborhood_adjacent_docs, depth=0)

        while True:
            # Select the next batch of nodes, and (new) outgoing edges.
            next_outgoing_edges = state.select_next_edges()
            if next_outgoing_edges is None:
                break
            elif next_outgoing_edges:
                # Find the (new) document with incoming edges from those edges.
                adjacent_docs = self._get_adjacent(
                    outgoing_edges=next_outgoing_edges,
                    query_embedding=query_embedding,
                    k_per_edge=adjacent_k,
                    filter=filter,
                    **store_kwargs,
                )

                state.add_docs(adjacent_docs)

        return state.finish()

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        initial_roots: Sequence[str] = (),
        k: int | None = None,
        start_k: int | None = None,
        adjacent_k: int | None = None,
        filter: dict[str, Any] | None = None,
        store_kwargs: dict[str, Any] = {},
        **kwargs: Any,
    ) -> list[Document]:
        """Asynchronously retrieve documents from this graph store using MMR-traversal.

        This strategy first retrieves the top `start_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.
        At each step, it considers the (remaining) documents from `start_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").

        Args:
            query: The query string to search for.
            initial_roots: Optional list of document IDs to use for initializing search.
                The top `adjacent_k` nodes adjacent to each initial root will be
                included in the set of initial candidates. To fetch only in the
                neighborhood of these nodes, set `start_k = 0`.
            k: Number of Documents to return. Defaults to 4.
            start_k: Number of initial Documents to fetch via similarity.
                Will be added to the nodes adjacent to `initial_roots`.
                Defaults to 100.
            adjacent_k: Number of adjacent Documents to fetch.
                Defaults to 10.
            filter: Optional metadata to filter the results.
            store_kwargs: Optional kwargs passed to queries to the store.
            **kwargs: Additional keyword arguments.
        """
        k = self.k if k is None else k
        start_k = self.start_k if start_k is None else start_k
        adjacent_k = self.adjacent_k if adjacent_k is None else adjacent_k

        # Retrieve initial candidates and initialize state.
        query_embedding, initial_docs = await self._afetch_initial_candidates(
            query, fetch_k=start_k, filter=filter, **store_kwargs
        )
        state = _TraversalState(
            k=k,
            node_selector_factory=self.node_selector_factory,
            query_embedding=query_embedding,
            edge_helper=self.edge_helper,
            **{**self.extra_args, **kwargs},
        )
        state.add_docs(initial_docs, depth=0)

        if initial_roots:
            neighborhood_adjacent_docs = await self._afetch_neighborhood_candidates(
                initial_roots,
                query_embedding=query_embedding,
                adjacent_k=adjacent_k,
                state=state,
                filter=filter,
                **store_kwargs,
            )
            state.add_docs(neighborhood_adjacent_docs, depth=0)

        while True:
            # Select the next batch of nodes, and (new) outgoing edges.
            next_outgoing_edges = state.select_next_edges()
            if next_outgoing_edges is None:
                break
            elif next_outgoing_edges:
                # Find the (new) document with incoming edges from those edges.
                adjacent_docs = await self._aget_adjacent(
                    outgoing_edges=next_outgoing_edges,
                    query_embedding=query_embedding,
                    k_per_edge=adjacent_k,
                    filter=filter,
                    **store_kwargs,
                )

                state.add_docs(adjacent_docs)

        return state.finish()

    def _fetch_initial_candidates(
        self, query: str, *, fetch_k: int, filter: dict[str, Any] | None, **kwargs: Any
    ) -> tuple[list[float], Iterable[Document]]:
        """Gets the embedded query and the set of initial candidates.

        Args:
            query: String to compute embedding and fetch initial matches for.
            fetch_k: Number of initial documents to fetch. If 0, no initial candidates
                will be fetched.
            filter: Optional metadata filter to apply.
            **kwargs: Additional keyword arguments.
        """
        query_embedding, docs = self.store.similarity_search_with_embedding(
            query=query,
            k=fetch_k,
            filter=filter,
            **kwargs,
        )
        return query_embedding, docs

    def _fetch_neighborhood_candidates(
        self,
        neighborhood: Sequence[str],
        *,
        query_embedding: list[float],
        adjacent_k: int,
        state: _TraversalState,
        filter: dict[str, Any] | None,
        **kwargs: Any,
    ):
        neighborhood_docs = self.store.get(neighborhood)
        neighborhood_nodes = state.add_docs(neighborhood_docs)

        # Record the neighborhood nodes (specifically the outgoing edges from the
        # neighborhood) as visited.
        outgoing_edges = state.visit_nodes(neighborhood_nodes.values())

        # Fetch the candidates.
        return self._get_adjacent(
            outgoing_edges=outgoing_edges,
            query_embedding=query_embedding,
            k_per_edge=adjacent_k,
            filter=filter,
            **kwargs,
        )

    async def _afetch_initial_candidates(
        self,
        query: str,
        *,
        fetch_k: int,
        filter: dict[str, Any] | None,
        **kwargs: Any,
    ) -> tuple[list[float], Iterable[Document]]:
        query_embedding, docs = await self.store.asimilarity_search_with_embedding(
            query=query,
            k=fetch_k,
            filter=filter,
            **kwargs,
        )
        return query_embedding, docs

    async def _afetch_neighborhood_candidates(
        self,
        neighborhood: Sequence[str],
        *,
        query_embedding: list[float],
        adjacent_k: int,
        state: _TraversalState,
        filter: dict[str, Any] | None,
        **kwargs: Any,
    ):
        neighborhood_docs = await self.store.aget(neighborhood)
        neighborhood_nodes = state.add_docs(neighborhood_docs)

        # Record the neighborhood nodes (specifically the outgoing edges from the
        # neighborhood) as visited.
        outgoing_edges = state.visit_nodes(neighborhood_nodes.values())

        # Fetch the candidates.
        return await self._aget_adjacent(
            outgoing_edges=outgoing_edges,
            query_embedding=query_embedding,
            k_per_edge=adjacent_k,
            filter=filter,
            **kwargs,
        )

    def _get_adjacent(
        self,
        outgoing_edges: set[Edge],
        query_embedding: list[float],
        k_per_edge: int | None = None,
        filter: dict[str, Any] | None = None,  # noqa: A002
        **kwargs: Any,
    ) -> Iterable[Document]:
        """Return the target docs with incoming edges from any of the given edges.
        Args:
            edges: The edges to look for.
            query_embedding: The query embedding. Used to rank target docs.
            doc_cache: A cache of retrieved docs. This will be added to.
            k_per_edge: The number of target docs to fetch for each edge.
            filter: Optional metadata to filter the results.
        Returns:
            Dictionary of adjacent nodes, keyed by node ID.
        """
        results: list[Document] = []
        for outgoing_edge in outgoing_edges:
            docs = self.store.similarity_search_with_embedding_by_vector(
                embedding=query_embedding,
                k=k_per_edge or 10,
                filter=self.edge_helper.get_metadata_filter(
                    base_filter=filter, edge=outgoing_edge
                ),
                **kwargs,
            )
            results.extend(docs)
        return results

    async def _aget_adjacent(
        self,
        outgoing_edges: set[Edge],
        query_embedding: list[float],
        k_per_edge: int | None = None,
        filter: dict[str, Any] | None = None,  # noqa: A002
        **kwargs: Any,
    ) -> Iterable[Document]:
        """Returns document nodes with incoming edges from any of the given edges.
        Args:
            edges: The edges to look for.
            query_embedding: The query embedding. Used to rank target nodes.
            k_per_edge: The number of target nodes to fetch for each edge.
            filter: Optional metadata to filter the results.
        Returns:
            Dictionary of adjacent nodes, keyed by node ID.
        """

        tasks = [
            self.store.asimilarity_search_with_embedding_by_vector(
                embedding=query_embedding,
                k=k_per_edge or 10,
                filter=self.edge_helper.get_metadata_filter(
                    base_filter=filter, edge=outgoing_edge
                ),
                **kwargs,
            )
            for outgoing_edge in outgoing_edges
        ]

        results: list[Document] = []
        for completed_task in asyncio.as_completed(tasks):
            docs = await completed_task
            results.extend(docs)
        return results
